"""Tests pentru HmacChainSpanProcessor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.processors import HmacChainSpanProcessor, verify_chain

SECRET = b"x" * 32


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "chain.db"


@pytest.fixture
def provider_with_chain(db_path: Path) -> TracerProvider:
    """TracerProvider cu HmacChainSpanProcessor."""
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    )
    trace.set_tracer_provider(provider)
    return provider


def _emit_genai_span(
    name: str = "anthropic.chat", model: str = "claude-haiku-4-5"
) -> None:
    """Helper: emite un span cu gen_ai.* attrs (trece filter-ul default)."""
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.usage.input_tokens", 10)
        span.set_attribute(
            "gen_ai.input.messages",
            '[{"role":"user","parts":[{"type":"text","content":"hi"}]}]',
        )


def test_processor_writes_row_per_filtered_span(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """Un span gen_ai.* -> un row în chain."""
    _emit_genai_span()
    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM chain").fetchone()
        assert rows[0] == 1


def test_processor_skips_non_genai_spans(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """Spans fără gen_ai.* attrs nu sunt sigilate."""
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("http.request") as span:
        span.set_attribute("http.method", "GET")
    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM chain").fetchone()
        assert rows[0] == 0


def test_chain_links_correctly(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """prev_hash[N] == hmac_hash[N-1]."""
    for _ in range(5):
        _emit_genai_span()
    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT seq, prev_hash, hmac_hash FROM chain ORDER BY seq"
        ).fetchall()

    assert rows[0][1] == "0" * 64  # Genesis prev_hash
    for i in range(1, len(rows)):
        assert rows[i][1] == rows[i - 1][2]


def test_verify_chain_passes_on_intact_chain(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """100 spans -> verify_chain returnează (True, None, None)."""
    for _ in range(100):
        _emit_genai_span()
    provider_with_chain.shutdown()

    valid, seq, reason = verify_chain(db_path, SECRET)
    assert valid is True
    assert seq is None
    assert reason is None


def test_verify_chain_detects_body_mutation(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """Mutație 1 byte în canonical_body -> verify_chain detectează."""
    for _ in range(10):
        _emit_genai_span()
    provider_with_chain.shutdown()

    # Mutație: alterăm canonical_body la rândul 5
    with sqlite3.connect(db_path) as conn:
        body = conn.execute(
            "SELECT canonical_body FROM chain WHERE seq = 5"
        ).fetchone()[0]
        mutated = body[:-1] + bytes([body[-1] ^ 0x01])  # Flip 1 bit
        conn.execute(
            "UPDATE chain SET canonical_body = ? WHERE seq = 5", (mutated,)
        )
        conn.commit()

    valid, seq, reason = verify_chain(db_path, SECRET)
    assert valid is False
    assert seq == 5
    assert reason is not None
    assert "canonical_hash mismatch" in reason


def test_verify_chain_detects_hash_swap(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """Swap canonical_hash între două row-uri -> detectat la rândul mutat."""
    for _ in range(10):
        _emit_genai_span()
    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        h2 = conn.execute(
            "SELECT canonical_hash FROM chain WHERE seq = 2"
        ).fetchone()[0]
        conn.execute(
            "UPDATE chain SET canonical_hash = ? WHERE seq = 5", (h2,)
        )
        conn.commit()

    valid, seq, reason = verify_chain(db_path, SECRET)
    assert valid is False
    assert seq == 5


def test_verify_chain_detects_wrong_secret(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """Verify cu secret greșit -> fail la primul row."""
    for _ in range(3):
        _emit_genai_span()
    provider_with_chain.shutdown()

    valid, seq, reason = verify_chain(db_path, b"y" * 32)
    assert valid is False
    assert seq == 1
    assert reason is not None
    assert "hmac_hash mismatch" in reason


def test_secret_key_min_length_enforced(db_path: Path) -> None:
    """Secret < 16 bytes -> ValueError."""
    with pytest.raises(ValueError, match="at least 16 bytes"):
        HmacChainSpanProcessor(db_path=db_path, secret_key=b"short")


def test_custom_filter_fn(db_path: Path) -> None:
    """filter_fn override permite sigilarea altor spans."""

    def keep_all(span: object) -> bool:
        return True

    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(
            db_path=db_path,
            secret_key=SECRET,
            filter_fn=keep_all,
        )
    )
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("http.request") as span:
        span.set_attribute("http.method", "GET")

    provider.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM chain").fetchone()
        assert rows[0] == 1
