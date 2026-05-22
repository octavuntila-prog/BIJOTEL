"""Tests for F13 / Bijuteria #7 — Fingerprint layer (harvest from substrate-guard).

Covers:
- DeterministicFingerprinter: shape, determinism, similarity, hash, protocol_id
- SemanticFingerprinter: graceful optional-dependency handling
- _extract_text helper: 3 message shapes (OTel parts, Anthropic multipart, OpenAI string)
- FingerprintSpanProcessor: writes, schema, crash isolation, filter
- similarity_search: above/below threshold, encoder-mismatch skip, empty store
- fingerprint_canonical_body: backfill helper
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.layers.fingerprint import (
    DeterministicFingerprinter,
    FingerprintSpanProcessor,
    SemanticFingerprinter,
    _extract_text,
    fingerprint_canonical_body,
    similarity_search,
)

# === DeterministicFingerprinter ===


def test_det_fingerprint_shape_dtype() -> None:
    fp = DeterministicFingerprinter()
    emb = fp.fingerprint("hello world")
    assert emb.shape == (384,)
    assert emb.dtype == np.float32


def test_det_fingerprint_l2_normalized() -> None:
    fp = DeterministicFingerprinter()
    emb = fp.fingerprint("any text here")
    norm = np.linalg.norm(emb)
    assert abs(norm - 1.0) < 1e-5, f"expected L2 norm ~1.0, got {norm}"


def test_det_fingerprint_deterministic() -> None:
    """Same input → byte-identical embedding (no ML randomness)."""
    fp = DeterministicFingerprinter()
    a = fp.fingerprint("the quick brown fox")
    b = fp.fingerprint("the quick brown fox")
    assert (a == b).all()


def test_det_self_similarity_is_one() -> None:
    fp = DeterministicFingerprinter()
    emb = fp.fingerprint("anything")
    sim = fp.similarity(emb, emb)
    assert abs(sim - 1.0) < 1e-5


def test_det_different_inputs_orthogonal_ish() -> None:
    """SHA-256 expansion produces near-orthogonal vectors for different inputs."""
    fp = DeterministicFingerprinter()
    a = fp.fingerprint("first document")
    b = fp.fingerprint("completely unrelated content here")
    sim = fp.similarity(a, b)
    # Empirically ~0; allow loose bound since not truly random
    assert abs(sim) < 0.2


def test_det_batch_consistent_with_single() -> None:
    fp = DeterministicFingerprinter()
    docs = ["alpha", "beta", "gamma"]
    batch = fp.fingerprint_batch(docs)
    assert batch.shape == (3, 384)
    for i, d in enumerate(docs):
        single = fp.fingerprint(d)
        assert (batch[i] == single).all()


def test_det_document_hash_stable() -> None:
    fp = DeterministicFingerprinter()
    h1 = fp.document_hash("hello")
    h2 = fp.document_hash("hello")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex


def test_det_protocol_id_format() -> None:
    fp = DeterministicFingerprinter()
    assert fp.protocol_id == "det:deterministic-sha256-v1:dim384:l2norm"


# === SemanticFingerprinter (optional dep) ===


def test_semantic_protocol_id_format() -> None:
    """Instantiation succeeds without sentence-transformers (lazy load)."""
    fp = SemanticFingerprinter()
    assert fp.protocol_id == "sbert:all-MiniLM-L6-v2:dim384:normalized"


def test_semantic_missing_dep_raises_actionable_importerror() -> None:
    """Calling fingerprint() without sentence-transformers installed → clear error."""
    fp = SemanticFingerprinter()
    # Force-block sentence_transformers import to simulate missing extra
    with (
        patch.dict(sys.modules, {"sentence_transformers": None}),
        pytest.raises(ImportError, match=r"pip install bijotel\[fingerprint\]"),
    ):
        fp.fingerprint("anything")


def test_semantic_custom_model_name_in_protocol_id() -> None:
    fp = SemanticFingerprinter(model_name="custom-model-v2")
    assert "custom-model-v2" in fp.protocol_id


# === _extract_text helper (3 message shapes) ===


def _make_span_with_messages(messages):
    """Build a minimal ReadableSpan-like object for _extract_text testing."""

    class _FakeSpan:
        attributes = {
            "gen_ai.request.model": "test",
            "gen_ai.input.messages": (
                messages if isinstance(messages, str) else json.dumps(messages)
            ),
        }

    return _FakeSpan()


def test_extract_text_openai_string_content() -> None:
    span = _make_span_with_messages([{"role": "user", "content": "hello there"}])
    assert _extract_text(span) == "hello there"


def test_extract_text_anthropic_multipart() -> None:
    span = _make_span_with_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "alpha"},
                    {"type": "text", "text": "beta"},
                ],
            }
        ]
    )
    assert _extract_text(span) == "alpha beta"


def test_extract_text_otel_parts_format() -> None:
    """OTel semconv shape: parts=[{content, type}]."""
    span = _make_span_with_messages(
        [{"role": "user", "parts": [{"content": "gamma", "type": "text"}]}]
    )
    assert _extract_text(span) == "gamma"


def test_extract_text_empty_messages_returns_empty() -> None:
    span = _make_span_with_messages([])
    assert _extract_text(span) == ""


def test_extract_text_missing_attribute_returns_empty() -> None:
    class _NoMessagesSpan:
        attributes = {"gen_ai.request.model": "x"}

    assert _extract_text(_NoMessagesSpan()) == ""


# === FingerprintSpanProcessor ===


@pytest.fixture
def fp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "fp.db"


def _emit_genai_span(text: str = "hello fingerprint world") -> None:
    """Helper: emit gen_ai span carrying text the processor will fingerprint."""
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("anthropic.chat") as span:
        span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        span.set_attribute(
            "gen_ai.input.messages",
            json.dumps([{"role": "user", "content": text}]),
        )


def test_fingerprint_processor_writes_row_per_filtered_span(fp_db_path: Path) -> None:
    """One gen_ai span → one fingerprints row."""
    provider = TracerProvider()
    provider.add_span_processor(FingerprintSpanProcessor(db_path=fp_db_path))
    trace.set_tracer_provider(provider)
    _emit_genai_span("first prompt text")
    provider.shutdown()

    with sqlite3.connect(fp_db_path) as conn:
        rows = conn.execute(
            "SELECT span_id, encoder, doc_hash, length(embedding) FROM fingerprints"
        ).fetchall()
    assert len(rows) == 1
    span_id, encoder, doc_hash, emb_len = rows[0]
    assert len(span_id) == 16
    assert encoder == "det:deterministic-sha256-v1:dim384:l2norm"
    assert len(doc_hash) == 64
    assert emb_len == 384 * 4  # float32 little-endian


def test_fingerprint_processor_skips_non_genai_spans(fp_db_path: Path) -> None:
    provider = TracerProvider()
    provider.add_span_processor(FingerprintSpanProcessor(db_path=fp_db_path))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("http.request") as span:
        span.set_attribute("http.method", "GET")
    provider.shutdown()

    with sqlite3.connect(fp_db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    assert n == 0


def test_fingerprint_processor_skips_empty_text(fp_db_path: Path) -> None:
    """Span with gen_ai.* but no extractable text → no fingerprint row."""
    provider = TracerProvider()
    provider.add_span_processor(FingerprintSpanProcessor(db_path=fp_db_path))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("anthropic.chat") as span:
        span.set_attribute("gen_ai.request.model", "test")
        # NO gen_ai.input.messages → no text
    provider.shutdown()

    with sqlite3.connect(fp_db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    assert n == 0


def test_fingerprint_processor_crash_isolated(
    fp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """sqlite write failure in on_end MUST be caught + logged, NOT propagated."""
    processor = FingerprintSpanProcessor(db_path=fp_db_path)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    with patch.object(
        processor,
        "_connect_for_write",
        side_effect=sqlite3.OperationalError("simulated"),
    ):
        caplog.set_level(logging.ERROR, logger="bijotel.fingerprint")
        # Must not raise
        _emit_genai_span("text that would fingerprint")
        provider.shutdown()

    assert any(
        "fingerprint write failed" in r.message and "OperationalError" in r.message
        for r in caplog.records
    )


def test_fingerprint_processor_idempotent_init(fp_db_path: Path) -> None:
    """Re-initializing the processor against existing db is idempotent."""
    FingerprintSpanProcessor(db_path=fp_db_path)  # creates table
    FingerprintSpanProcessor(db_path=fp_db_path)  # no-op (IF NOT EXISTS)
    with sqlite3.connect(fp_db_path) as conn:
        # Schema check
        cols = [r[1] for r in conn.execute("PRAGMA table_info(fingerprints)").fetchall()]
        assert set(cols) >= {
            "span_id", "trace_id", "encoder", "embedding", "doc_hash", "created_ns"
        }


# === similarity_search ===


def _populate_fp_db(db_path: Path, texts: list[str]) -> None:
    """Helper: emit spans for each text via FingerprintSpanProcessor."""
    provider = TracerProvider()
    provider.add_span_processor(FingerprintSpanProcessor(db_path=db_path))
    trace.set_tracer_provider(provider)
    for t in texts:
        _emit_genai_span(t)
    provider.shutdown()


def test_similarity_search_finds_exact_match(fp_db_path: Path) -> None:
    """Query identical to an ingested doc → similarity 1.0."""
    _populate_fp_db(fp_db_path, ["target document for matching"])
    results = similarity_search(
        fp_db_path, "target document for matching", threshold=0.9
    )
    assert len(results) == 1
    assert results[0]["similarity"] > 0.999


def test_similarity_search_misses_unrelated(fp_db_path: Path) -> None:
    """Unrelated query → no matches above threshold."""
    _populate_fp_db(fp_db_path, ["doc A about something"])
    results = similarity_search(
        fp_db_path, "totally different unrelated content", threshold=0.5
    )
    # SHA-256 expansion produces near-orthogonal → similarity < 0.2 < 0.5
    assert len(results) == 0


def test_similarity_search_respects_threshold(fp_db_path: Path) -> None:
    """Lower threshold → more matches (including marginal ones)."""
    _populate_fp_db(fp_db_path, ["alpha", "beta", "gamma"])
    high = similarity_search(fp_db_path, "alpha", threshold=0.99)
    low = similarity_search(fp_db_path, "alpha", threshold=-1.0)
    assert len(high) == 1  # only exact match
    assert len(low) == 3  # all return since threshold = -1.0


def test_similarity_search_encoder_mismatch_skipped(fp_db_path: Path) -> None:
    """Spans ingested with encoder X aren't returned by query encoder Y."""
    _populate_fp_db(fp_db_path, ["sample text"])
    # Query with a different (fake) protocol_id → encoder mismatch, skip all
    fake_fp = DeterministicFingerprinter()
    fake_fp.ENCODER = "totally-different-encoder-v999"  # type: ignore[misc]
    results = similarity_search(
        fp_db_path, "sample text", threshold=-1.0, fingerprinter=fake_fp
    )
    assert len(results) == 0


def test_similarity_search_empty_db(fp_db_path: Path) -> None:
    """Empty store → empty results, no crash."""
    FingerprintSpanProcessor(db_path=fp_db_path)  # create empty table
    results = similarity_search(fp_db_path, "anything", threshold=0.0)
    assert results == []


# === fingerprint_canonical_body backfill helper ===


def test_fingerprint_canonical_body_extracts_text() -> None:
    """Backfill helper works on canonical_body BLOB shape."""
    body = json.dumps(
        {
            "attributes": {
                "gen_ai.input.messages": [
                    {"role": "user", "content": "backfilled prompt"}
                ]
            }
        }
    ).encode()
    result = fingerprint_canonical_body(body)
    assert result is not None
    emb, doc_hash = result
    assert emb.shape == (384,)
    assert len(doc_hash) == 64


def test_fingerprint_canonical_body_returns_none_on_no_text() -> None:
    body = json.dumps({"attributes": {}}).encode()
    assert fingerprint_canonical_body(body) is None
