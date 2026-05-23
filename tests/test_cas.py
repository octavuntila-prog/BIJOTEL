"""Tests pentru CasSpanProcessor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.processors import CasSpanProcessor, cas_lookup, cas_stats


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cas.db"


@pytest.fixture
def provider_with_cas(db_path: Path) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(CasSpanProcessor(db_path=db_path))
    trace.set_tracer_provider(provider)
    return provider


def _emit_genai_span(input_text: str = "hi", output_text: str = "ok") -> None:
    """Helper: emite span cu input/output configurabil (input variază -> CAS dedup)."""
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("anthropic.chat") as span:
        span.set_attribute("gen_ai.request.model", "claude")
        span.set_attribute(
            "gen_ai.input.messages",
            f'[{{"role":"user","parts":[{{"type":"text","content":"{input_text}"}}]}}]',
        )
        span.set_attribute(
            "gen_ai.output.messages",
            f'[{{"role":"assistant","parts":[{{"type":"text","content":"{output_text}"}}]}}]',
        )
        span.set_attribute("gen_ai.usage.input_tokens", 5)
        span.set_attribute("gen_ai.usage.output_tokens", 10)


def test_cas_writes_entry_per_unique_input(
    provider_with_cas: TracerProvider, db_path: Path
) -> None:
    """3 spans cu input distinct -> 3 CAS entries."""
    _emit_genai_span("input_1")
    _emit_genai_span("input_2")
    _emit_genai_span("input_3")
    provider_with_cas.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM cas").fetchone()
        assert rows[0] == 3


def test_cas_dedup_on_identical_input(
    provider_with_cas: TracerProvider, db_path: Path
) -> None:
    """3 spans cu input identic dar output DIFERIT -> 1 CAS entry, ref_count=3."""
    _emit_genai_span("identical_input", output_text="output_A")
    _emit_genai_span("identical_input", output_text="output_B")
    _emit_genai_span("identical_input", output_text="output_C")
    provider_with_cas.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT body_hash, ref_count FROM cas").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == 3, f"Expected ref_count=3, got {rows[0][1]}"


def test_cas_skips_non_genai_spans(
    provider_with_cas: TracerProvider, db_path: Path
) -> None:
    """Spans fără gen_ai.* nu sunt stocate."""
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("http.request") as span:
        span.set_attribute("http.method", "GET")
    provider_with_cas.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM cas").fetchone()
        assert rows[0] == 0


def test_cas_lookup_returns_body(
    provider_with_cas: TracerProvider, db_path: Path
) -> None:
    """cas_lookup returnează body + metadata pentru hash existent."""
    _emit_genai_span("test_input")
    provider_with_cas.shutdown()

    with sqlite3.connect(db_path) as conn:
        body_hash = conn.execute("SELECT body_hash FROM cas").fetchone()[0]

    result = cas_lookup(db_path, body_hash)
    assert result is not None
    body, _first_seen, ref_count = result
    assert b"test_input" in body
    assert ref_count == 1


def test_cas_lookup_returns_none_for_missing_hash(db_path: Path) -> None:
    """cas_lookup pe hash inexistent -> None."""
    # init empty CAS
    provider = TracerProvider()
    provider.add_span_processor(CasSpanProcessor(db_path=db_path))
    trace.set_tracer_provider(provider)
    provider.shutdown()

    result = cas_lookup(db_path, "0" * 64)
    assert result is None


def test_cas_stats(provider_with_cas: TracerProvider, db_path: Path) -> None:
    """cas_stats: 2 unique bodies, 5 total refs."""
    _emit_genai_span("a")
    _emit_genai_span("a")
    _emit_genai_span("a")
    _emit_genai_span("b")
    _emit_genai_span("b")
    provider_with_cas.shutdown()

    stats = cas_stats(db_path)
    assert stats["unique_bodies"] == 2
    assert stats["total_refs"] == 5
    assert stats["dedup_factor"] == 2.5


# ═══════════════════════════════════════════════════════════════════════
# v1.5.3 — Merkle DAG wire (Bijuteria #2 sub-layer activated)
# ═══════════════════════════════════════════════════════════════════════


def _dag_row_count(db: Path, table: str) -> int:
    with sqlite3.connect(db) as conn:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return -1  # table absent


def test_v153_cas_creates_dag_node_by_default(db_path: Path) -> None:
    """A span flowing through CasSpanProcessor now also populates dag_nodes."""
    provider = TracerProvider()
    provider.add_span_processor(CasSpanProcessor(db_path=db_path))
    trace.set_tracer_provider(provider)
    _emit_genai_span("dag-test-1")
    provider.shutdown()

    cas_count = _dag_row_count(db_path, "cas")
    dag_count = _dag_row_count(db_path, "dag_nodes")
    assert cas_count == 1
    assert dag_count == 1, "DAG node should be created alongside CAS row"


def test_v153_cas_dag_dedup_no_duplicate_nodes(db_path: Path) -> None:
    """Same body_hash twice → 1 CAS row (ref_count=2), 1 DAG node."""
    provider = TracerProvider()
    provider.add_span_processor(CasSpanProcessor(db_path=db_path))
    trace.set_tracer_provider(provider)
    _emit_genai_span("same")
    _emit_genai_span("same")
    provider.shutdown()

    assert _dag_row_count(db_path, "cas") == 1
    assert _dag_row_count(db_path, "dag_nodes") == 1


def test_v153_cas_enable_dag_false_keeps_v15x_behavior(db_path: Path) -> None:
    """Backwards compat — enable_dag=False does NOT create dag_nodes."""
    provider = TracerProvider()
    provider.add_span_processor(
        CasSpanProcessor(db_path=db_path, enable_dag=False)
    )
    trace.set_tracer_provider(provider)
    _emit_genai_span("no-dag-test")
    provider.shutdown()

    assert _dag_row_count(db_path, "cas") == 1
    # Table either absent (-1) or empty (0) — both mean DAG inactive
    assert _dag_row_count(db_path, "dag_nodes") in (-1, 0)


def test_v153_cas_multiple_distinct_bodies_each_get_dag_node(db_path: Path) -> None:
    """Three different input prompts → 3 CAS rows + 3 DAG nodes."""
    provider = TracerProvider()
    provider.add_span_processor(CasSpanProcessor(db_path=db_path))
    trace.set_tracer_provider(provider)
    for input_text in ("alpha", "beta", "gamma"):
        _emit_genai_span(input_text)
    provider.shutdown()

    assert _dag_row_count(db_path, "cas") == 3
    assert _dag_row_count(db_path, "dag_nodes") == 3


def test_v153_dag_refs_table_empty_v153_no_cross_span_refs(db_path: Path) -> None:
    """v1.5.3 leaves refs empty — cross-span ref logic deferred to v1.6+.

    The denormalized `dag_refs` table stays empty in v1.5.3 because
    `add_node(refs=[])` is what CasSpanProcessor passes.
    """
    provider = TracerProvider()
    provider.add_span_processor(CasSpanProcessor(db_path=db_path))
    trace.set_tracer_provider(provider)
    _emit_genai_span("solo")
    provider.shutdown()

    assert _dag_row_count(db_path, "dag_nodes") == 1
    assert _dag_row_count(db_path, "dag_refs") == 0
