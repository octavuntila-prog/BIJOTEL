"""Tests for RAG source provenance (v2.6.0).

Covers the public surface in ``bijotel.rag`` plus its interactions with:

- canonical_dict (RAG attrs survive seal + verify roundtrip)
- semantic exclude list (sources JSON excluded from CAS dedup, the rest
  stays in the dedup key so identical-retriever calls still match)
- F12 regression dimension ``rag_source_count``
- the inspect CLI helper that pretty-prints sources

The decorator path uses a real ``TracerProvider`` so we cover the actual
contextvar propagation, not a mock.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.cli.cmd_chain import _print_rag_sources
from bijotel.processors import HmacChainSpanProcessor, verify_chain
from bijotel.processors.canonical import (
    SEMANTIC_EXCLUDE_ATTRS,
    span_to_canonical_dict,
    span_to_semantic_dict,
)
from bijotel.rag import RAGSource, rag_context, with_rag_provenance
from bijotel.regression.baseline import (
    VALID_DIMENSIONS,
    _extract_dimension_value,
)

SECRET = b"x" * 32


def _sample_sources(n: int = 2) -> list[RAGSource]:
    """Return ``n`` RAGSource records with reproducible fields."""
    return [
        RAGSource(
            document_id=f"sha256:{'a' * 60}{i:04d}",
            chunk_index=i,
            source_uri=f"s3://corpus/doc-{i}.pdf",
            retriever="qdrant",
            embedding_model="text-embedding-3-small",
            similarity_score=0.9 - i * 0.05,
            retrieved_at="2026-05-26T10:00:00Z",
        )
        for i in range(n)
    ]


# ----------------------------------------------------------------------
# 1. Dataclass
# ----------------------------------------------------------------------


def test_rag_source_dataclass_defaults() -> None:
    """All optional fields default cleanly; only the two required ones must be set."""
    s = RAGSource(document_id="sha256:abc", chunk_index=0)
    assert s.document_id == "sha256:abc"
    assert s.chunk_index == 0
    assert s.source_uri == ""
    assert s.retriever == ""
    assert s.embedding_model == ""
    assert s.similarity_score == 0.0
    assert s.retrieved_at == ""
    assert s.metadata == {}


def test_rag_source_to_dict_roundtrip() -> None:
    """``to_dict`` produces JSON-serializable output that round-trips."""
    s = _sample_sources(1)[0]
    d = s.to_dict()
    assert isinstance(d, dict)
    assert d["document_id"] == s.document_id
    # JSON round-trip — proves no non-serializable types snuck in.
    assert json.loads(json.dumps(d)) == d


def test_rag_source_is_frozen() -> None:
    """The dataclass is frozen — mutating in place must raise."""
    s = RAGSource(document_id="sha256:abc", chunk_index=0)
    with pytest.raises((AttributeError, TypeError)):
        s.document_id = "tampered"  # type: ignore[misc]


# ----------------------------------------------------------------------
# 2. rag_context()
# ----------------------------------------------------------------------


def test_rag_context_single_source() -> None:
    """One source → all four attrs populated, retriever/embedding from it."""
    sources = _sample_sources(1)
    attrs = rag_context(sources)
    assert attrs["bijotel.rag.source_count"] == 1
    assert attrs["bijotel.rag.retriever_id"] == "qdrant"
    assert attrs["bijotel.rag.embedding_model"] == "text-embedding-3-small"
    decoded = json.loads(attrs["bijotel.rag.sources"])
    assert len(decoded) == 1
    assert decoded[0]["document_id"].startswith("sha256:")


def test_rag_context_multiple_sources() -> None:
    """Multiple sources → count matches, primary retriever from first."""
    sources = _sample_sources(3)
    attrs = rag_context(sources)
    assert attrs["bijotel.rag.source_count"] == 3
    assert attrs["bijotel.rag.retriever_id"] == "qdrant"
    decoded = json.loads(attrs["bijotel.rag.sources"])
    assert len(decoded) == 3
    # Similarity scores preserved in order.
    assert decoded[0]["similarity_score"] > decoded[1]["similarity_score"]


def test_rag_context_empty_sources() -> None:
    """Empty list still emits attrs (count=0); distinguishes 'tried, got 0' from 'didn't try'."""
    attrs = rag_context([])
    assert attrs["bijotel.rag.source_count"] == 0
    assert attrs["bijotel.rag.sources"] == "[]"
    assert attrs["bijotel.rag.retriever_id"] == ""
    assert attrs["bijotel.rag.embedding_model"] == ""


def test_rag_context_optional_token_count() -> None:
    """``total_context_tokens`` only appears in the attrs dict when provided."""
    a = rag_context(_sample_sources(1))
    assert "bijotel.rag.total_context_tokens" not in a

    b = rag_context(_sample_sources(1), total_context_tokens=1234)
    assert b["bijotel.rag.total_context_tokens"] == 1234


def test_rag_context_sources_is_json_string_not_list() -> None:
    """OTel attributes must be primitives. Sources must be JSON-encoded."""
    attrs = rag_context(_sample_sources(2))
    raw = attrs["bijotel.rag.sources"]
    assert isinstance(raw, str)
    # Deterministic ordering — sort_keys=True for stable HMAC hashing.
    assert "document_id" in raw
    parsed = json.loads(raw)
    assert isinstance(parsed, list)


# ----------------------------------------------------------------------
# 3. Decorator
# ----------------------------------------------------------------------


def test_with_rag_provenance_attaches_attrs_to_span(tmp_path: Path) -> None:
    """Decorator attaches all attrs to the currently active span."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db, secret_key=SECRET)
    )
    trace.set_tracer_provider(provider)

    sources = _sample_sources(2)

    @with_rag_provenance(sources, total_context_tokens=500)
    def call_llm() -> str:
        return "answer"

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("test-rag") as span:
        span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        call_llm()

    provider.shutdown()

    # Read the sealed row back and check attrs.
    with sqlite3.connect(db) as conn:
        body = conn.execute(
            "SELECT canonical_body FROM chain WHERE seq = 1"
        ).fetchone()[0]
    body_dict = json.loads(
        body.decode("utf-8") if isinstance(body, bytes) else body
    )
    attrs = body_dict["attributes"]
    assert attrs["bijotel.rag.source_count"] == 2
    assert attrs["bijotel.rag.retriever_id"] == "qdrant"
    assert attrs["bijotel.rag.total_context_tokens"] == 500
    # The JSON shape survives canonicalization.
    decoded = json.loads(attrs["bijotel.rag.sources"])
    assert len(decoded) == 2


def test_with_rag_provenance_passes_through_return_value() -> None:
    """Decorator must not swallow the wrapped function's result."""

    @with_rag_provenance(_sample_sources(1))
    def compute() -> int:
        return 42

    # Even with no active span (no TracerProvider configured here), the
    # decorator should be a transparent pass-through.
    assert compute() == 42


def test_with_rag_provenance_passes_args() -> None:
    """Wrapped function receives its args + kwargs unchanged."""

    @with_rag_provenance(_sample_sources(1))
    def echo(a: int, b: int, *, c: int = 0) -> tuple[int, int, int]:
        return (a, b, c)

    assert echo(1, 2, c=3) == (1, 2, 3)


# ----------------------------------------------------------------------
# 4. Canonicalization + verify roundtrip
# ----------------------------------------------------------------------


def test_rag_attrs_in_canonical_body() -> None:
    """span_to_canonical_dict captures every bijotel.rag.* attribute."""
    sources = _sample_sources(2)
    attrs_dict = rag_context(sources, total_context_tokens=800)

    span = MagicMock()
    span.name = "test"
    span.kind.name = "CLIENT"
    span.attributes = {
        "gen_ai.request.model": "claude-haiku-4-5",
        **attrs_dict,
    }
    span.status.status_code.name = "OK"
    span.status.description = None
    span.start_time = 1
    span.end_time = 2

    canonical = span_to_canonical_dict(span)
    body_attrs = canonical["attributes"]
    assert body_attrs["bijotel.rag.source_count"] == 2
    assert body_attrs["bijotel.rag.retriever_id"] == "qdrant"
    assert body_attrs["bijotel.rag.total_context_tokens"] == 800
    # sources JSON preserved as a string in the canonical body.
    assert isinstance(body_attrs["bijotel.rag.sources"], str)


def test_rag_attrs_survive_seal_and_verify(tmp_path: Path) -> None:
    """End-to-end: write 3 RAG-augmented spans, verify chain valid."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db, secret_key=SECRET)
    )
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test")
    for i in range(3):
        with tracer.start_as_current_span(f"rag-call-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
            span.set_attribute("gen_ai.usage.input_tokens", 100)
            span.set_attribute("gen_ai.usage.output_tokens", 50)
            for k, v in rag_context(_sample_sources(2)).items():
                span.set_attribute(k, v)
    provider.shutdown()

    ok, failed_seq, reason = verify_chain(db, SECRET)
    assert ok is True, f"chain should verify: failed_seq={failed_seq} reason={reason}"


# ----------------------------------------------------------------------
# 5. Semantic / CAS dedup
# ----------------------------------------------------------------------


def test_semantic_exclude_drops_sources_keeps_stable_attrs() -> None:
    """``bijotel.rag.sources`` is excluded from dedup; the other RAG attrs stay."""
    assert "bijotel.rag.sources" in SEMANTIC_EXCLUDE_ATTRS
    # The *stable* RAG attrs must NOT be in the exclude list — otherwise
    # different retrievers would collide in dedup.
    assert "bijotel.rag.retriever_id" not in SEMANTIC_EXCLUDE_ATTRS
    assert "bijotel.rag.embedding_model" not in SEMANTIC_EXCLUDE_ATTRS
    assert "bijotel.rag.source_count" not in SEMANTIC_EXCLUDE_ATTRS


def test_semantic_dict_strips_only_sources_json() -> None:
    """The semantic projection drops sources JSON but keeps retriever_id etc."""
    attrs_dict = rag_context(_sample_sources(2))
    span = MagicMock()
    span.name = "test"
    span.kind.name = "CLIENT"
    span.attributes = {
        "gen_ai.request.model": "claude-haiku-4-5",
        **attrs_dict,
    }
    span.status.status_code.name = "OK"
    span.status.description = None
    span.start_time = 1
    span.end_time = 2

    sem = span_to_semantic_dict(span)
    sem_attrs = sem["attributes"]
    assert "bijotel.rag.sources" not in sem_attrs
    assert sem_attrs["bijotel.rag.retriever_id"] == "qdrant"
    assert sem_attrs["bijotel.rag.source_count"] == 2


# ----------------------------------------------------------------------
# 6. F12 regression dimension
# ----------------------------------------------------------------------


def test_rag_source_count_in_valid_dimensions() -> None:
    """The new dimension is registered for the regression detector."""
    assert "rag_source_count" in VALID_DIMENSIONS


def test_extract_rag_source_count_present() -> None:
    """When the attribute is set, the extractor returns its float."""
    body = {"attributes": {"bijotel.rag.source_count": 3}}
    v = _extract_dimension_value(body, "rag_source_count")
    assert v == 3.0


def test_extract_rag_source_count_absent_returns_none() -> None:
    """Old chains without RAG attrs yield None — never raise."""
    body = {"attributes": {"gen_ai.request.model": "claude-haiku-4-5"}}
    v = _extract_dimension_value(body, "rag_source_count")
    assert v is None


# ----------------------------------------------------------------------
# 7. inspect CLI display
# ----------------------------------------------------------------------


def test_print_rag_sources_handles_missing_attrs(capsys) -> None:
    """No RAG attrs → silent (nothing printed)."""
    _print_rag_sources({"attributes": {"gen_ai.request.model": "x"}})
    captured = capsys.readouterr()
    assert captured.out == ""


def test_print_rag_sources_prints_table(capsys) -> None:
    """With RAG attrs, the inspect helper prints a recognizable block."""
    body = {
        "attributes": {
            **rag_context(_sample_sources(2), total_context_tokens=500),
        }
    }
    _print_rag_sources(body)
    out = capsys.readouterr().out
    assert "RAG Provenance" in out
    assert "source_count:   2" in out
    assert "qdrant" in out
    assert "context_tokens: 500" in out
    # Both sources rendered.
    assert "source 1:" in out
    assert "source 2:" in out


def test_print_rag_sources_truncates_long_lists(capsys) -> None:
    """More than 5 sources → first 5 shown, rest summarized as count."""
    body = {"attributes": rag_context(_sample_sources(8))}
    _print_rag_sources(body)
    out = capsys.readouterr().out
    assert "source 5:" in out
    assert "source 6:" not in out
    assert "and 3 more" in out


# ----------------------------------------------------------------------
# 8. Public API surface
# ----------------------------------------------------------------------


def test_public_api_exports() -> None:
    """The three RAG names are exported from the top-level package."""
    import bijotel

    for name in ("RAGSource", "rag_context", "with_rag_provenance"):
        assert hasattr(bijotel, name), f"bijotel.{name} missing from top-level"
        assert name in bijotel.__all__
