"""Tests for append_event: the non-span entry point to the HMAC chain (2.16.0).

The load-bearing test is parity with on_end (test_mixed_span_and_event_*):
rows sealed via append_event and rows sealed via on_end(span) must form ONE
chain that verify_chain accepts and that chain-links across the source
boundary. Parity is proven by exercising BOTH in the same chain.db, not by
checking each in isolation — because the only place an append-vs-span hash
divergence could hide is a chain that mixes the two.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider

from bijotel.processors import HmacChainSpanProcessor, append_event, verify_chain
from bijotel.processors.hmac_chain import GENESIS_HASH

SECRET = b"x" * 32


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "chain.db"


def _emit_gen_ai_span(provider: TracerProvider, name: str) -> None:
    """Emit one span that passes the default gen_ai.* filter (sealed on end)."""
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("gen_ai.request.model", "test-model")


def _rows(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT seq, span_name, prev_hash, hmac_hash, canonical_hash "
            "FROM chain ORDER BY seq ASC"
        ).fetchall()


def test_append_event_alone_verifies(db_path: Path) -> None:
    r1 = append_event(db_path, SECRET, {"type": "policy_decision", "agent_id": "a1"})
    r2 = append_event(db_path, SECRET, {"type": "verification", "agent_id": "a1"})

    valid, last_seq, reason = verify_chain(db_path, SECRET)
    assert valid is True, reason
    assert last_seq == 2

    rows = _rows(db_path)
    assert rows[0][2] == GENESIS_HASH          # first row prev == genesis
    assert rows[0][3] == r1["hmac_hash"]       # returned hmac matches stored
    assert rows[1][2] == r1["hmac_hash"]       # link: row2.prev == row1.hmac
    assert rows[1][3] == r2["hmac_hash"]


def test_append_event_first_row_prev_is_genesis(db_path: Path) -> None:
    r1 = append_event(db_path, SECRET, {"type": "x"})
    assert r1["prev_hash"] == GENESIS_HASH
    assert r1["seq"] == 1


def test_mixed_span_and_event_form_one_valid_chain(db_path: Path) -> None:
    """THE parity test: on_end rows + append_event rows interleaved in ONE chain.

    verify_chain must accept the whole thing AND every row's prev_hash must link
    to the previous row's hmac_hash regardless of which path sealed it.
    """
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    )

    _emit_gen_ai_span(provider, "span.1")                       # seq 1 via on_end
    append_event(db_path, SECRET, {"type": "evt", "n": 2})      # seq 2 via append_event
    append_event(db_path, SECRET, {"type": "evt", "n": 3})      # seq 3 via append_event
    _emit_gen_ai_span(provider, "span.4")                       # seq 4 via on_end
    append_event(db_path, SECRET, {"type": "evt", "n": 5})      # seq 5 via append_event

    provider.shutdown()

    valid, last_seq, reason = verify_chain(db_path, SECRET)
    assert valid is True, f"mixed chain broke: {reason}"
    assert last_seq == 5

    rows = _rows(db_path)
    assert len(rows) == 5
    # Every row links to its predecessor regardless of source path.
    expected_prev = GENESIS_HASH
    for seq, _name, prev_hash, hmac_hash, _ch in rows:
        assert prev_hash == expected_prev, f"link broke at seq={seq}"
        expected_prev = hmac_hash
    # Confirm the chain genuinely mixed both sources.
    names = [r[1] for r in rows]
    assert "span.1" in names, "span source missing"
    assert "audit.event" in names, "event source missing"


def test_append_event_continues_an_existing_span_chain(db_path: Path) -> None:
    """append_event after a span chain links onto the last span's hmac_hash."""
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    )
    _emit_gen_ai_span(provider, "span.only")
    provider.shutdown()

    span_rows = _rows(db_path)
    assert len(span_rows) == 1
    last_span_hmac = span_rows[0][3]

    r = append_event(db_path, SECRET, {"type": "after_span"})
    assert r["prev_hash"] == last_span_hmac, "event did not link onto span head"

    valid, _last, reason = verify_chain(db_path, SECRET)
    assert valid is True, reason


def test_append_event_rejects_short_secret(db_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 16 bytes"):
        append_event(db_path, b"short", {"type": "x"})


def test_append_event_tamper_detected(db_path: Path) -> None:
    append_event(db_path, SECRET, {"type": "a"})
    append_event(db_path, SECRET, {"type": "b"})
    # Mutate the canonical_body of row 1 directly — verify must catch it.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE chain SET canonical_body = ? WHERE seq = 1",
            (b'{"type":"TAMPERED"}',),
        )
        conn.commit()

    valid, fail_seq, _reason = verify_chain(db_path, SECRET)
    assert valid is False
    assert fail_seq == 1
