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
