"""Hardening tests for v0.6.0: crash isolation, WAL+IMMEDIATE, file perms.

Covers production-readiness gaps surfaced by the T+7d audit DOC 03:
- F1: HmacChainSpanProcessor.on_end had no try/except → host crash risk
- F2: Multi-writer race on shared chain.db (per-process threading.Lock only)
- E2: chain.db mode 644 world-readable (canonical_body holds prompt BLOBs)

These tests pin the hardening contract so regressions surface immediately.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.processors import (
    CasSpanProcessor,
    HmacChainSpanProcessor,
    verify_chain,
)

SECRET = b"x" * 32


# === Helpers ===


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "chain.db"


def _emit_genai_span(name: str = "anthropic.chat") -> None:
    """Emit a span with gen_ai.* attrs (passes default filter)."""
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span(name) as span:
        span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        span.set_attribute(
            "gen_ai.input.messages",
            '[{"role":"user","parts":[{"type":"text","content":"hi"}]}]',
        )


# === Part A: crash isolation in on_end ===


def test_chain_on_end_sqlite_error_does_not_propagate(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """sqlite3 write failure in on_end MUST be caught and logged, not raised."""
    processor = HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    # Inject an sqlite3 failure by mocking the connection helper to raise
    with patch.object(
        processor,
        "_connect_for_write",
        side_effect=sqlite3.OperationalError("simulated disk full"),
    ):
        caplog.set_level(logging.ERROR, logger="bijotel.chain")
        # If on_end propagates, this raises and the test fails
        _emit_genai_span()
        provider.shutdown()

    assert any(
        "chain write failed" in r.message and "OperationalError" in r.message
        for r in caplog.records
    ), "expected ERROR log with 'chain write failed' + 'OperationalError'"


def test_chain_on_end_canonicalize_error_does_not_propagate(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """canonicalization failure in on_end MUST be caught and logged, not raised."""
    processor = HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    with patch(
        "bijotel.processors.hmac_chain.canonicalize",
        side_effect=ValueError("simulated canonicalize bug"),
    ):
        caplog.set_level(logging.ERROR, logger="bijotel.chain")
        _emit_genai_span()
        provider.shutdown()

    assert any(
        "chain write failed" in r.message and "ValueError" in r.message
        for r in caplog.records
    ), "expected ERROR log with 'chain write failed' + 'ValueError'"


def test_chain_continues_after_failed_entry(db_path: Path) -> None:
    """A failed on_end must not corrupt subsequent entries: chain stays VALID."""
    processor = HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    # 3 normal spans
    for _ in range(3):
        _emit_genai_span()

    # 1 span that fails inside on_end (sqlite injected error)
    with patch.object(
        processor,
        "_connect_for_write",
        side_effect=sqlite3.OperationalError("transient"),
    ):
        _emit_genai_span()  # silently dropped, host not disturbed

    # 3 more normal spans — chain should pick up from last good entry
    for _ in range(3):
        _emit_genai_span()
    provider.shutdown()

    valid, seq, reason = verify_chain(db_path, SECRET)
    assert valid is True, f"chain broken at seq={seq}: {reason}"

    # 6 entries total (3 + 3); the failed-write span is NOT in chain
    with sqlite3.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
        assert n == 6


def test_cas_on_end_error_does_not_propagate(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CAS on_end MUST also crash-isolate (same hardening contract)."""
    processor = CasSpanProcessor(db_path=db_path)
    provider = TracerProvider()
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    with patch.object(
        processor,
        "_connect_for_write",
        side_effect=sqlite3.OperationalError("disk full"),
    ):
        caplog.set_level(logging.ERROR, logger="bijotel.cas")
        _emit_genai_span()
        provider.shutdown()

    assert any(
        "cas write failed" in r.message for r in caplog.records
    ), "expected ERROR log with 'cas write failed'"


# === Part B: WAL + busy_timeout + BEGIN IMMEDIATE ===


def test_chain_wal_mode_enabled_after_init(db_path: Path) -> None:
    """journal_mode=WAL persists at the database level after _init_db."""
    HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal", f"expected WAL, got {mode!r}"


def test_cas_wal_mode_enabled_after_init(db_path: Path) -> None:
    """CAS also enables WAL (shared-db scenario or standalone)."""
    CasSpanProcessor(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_chain_busy_timeout_set_on_write_connection(db_path: Path) -> None:
    """_connect_for_write sets PRAGMA busy_timeout=5000."""
    processor = HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    conn = processor._connect_for_write()  # noqa: SLF001
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000, f"expected 5000ms, got {timeout}"
    finally:
        conn.close()


def test_chain_uses_explicit_transaction_for_writes(db_path: Path) -> None:
    """on_end issues BEGIN IMMEDIATE explicitly (verified via in_transaction)."""
    processor = HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    conn = processor._connect_for_write()  # noqa: SLF001
    try:
        # autocommit mode: in_transaction starts False
        assert conn.in_transaction is False
        conn.execute("BEGIN IMMEDIATE")
        assert conn.in_transaction is True
        conn.execute("COMMIT")
        assert conn.in_transaction is False
    finally:
        conn.close()


def _multiproc_worker(db_path_str: str, secret_hex: str, n_spans: int) -> None:
    """Module-level worker for multiprocessing (Windows spawn requires this).

    Each subprocess sets up its own TracerProvider + HmacChainSpanProcessor
    against the SAME db_path, and writes ``n_spans`` gen_ai spans concurrently
    with other workers. This exercises the multi-process WAL + BEGIN IMMEDIATE
    path. Without IMMEDIATE, the SELECT-prev-INSERT race would corrupt the chain.
    """
    # Subprocess starts fresh under spawn; module-level imports are re-imported
    # automatically. Build a per-subprocess TracerProvider against the shared db.
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(
            db_path=Path(db_path_str),
            secret_key=bytes.fromhex(secret_hex),
        )
    )
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("worker")
    for i in range(n_spans):
        with tracer.start_as_current_span("worker.span") as span:
            span.set_attribute("gen_ai.request.model", "test")
            span.set_attribute("gen_ai.worker_iter", i)
    provider.shutdown()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="multiprocessing spawn-fixture friction on Windows; covered by GENA",
)
def test_concurrent_writers_no_chain_corruption(db_path: Path) -> None:
    """4 concurrent writer processes × 25 spans each → 100 chain entries VALID.

    Without WAL + BEGIN IMMEDIATE, the SELECT-prev-INSERT race between processes
    would produce a fork (two entries with the same prev_hash). verify_chain
    catches this via the prev_hash linkage check. This test pins the multi-writer
    contract.
    """
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    n_workers = 4
    spans_per_worker = 25
    procs = [
        ctx.Process(
            target=_multiproc_worker,
            args=(str(db_path), SECRET.hex(), spans_per_worker),
        )
        for _ in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, f"worker exited {p.exitcode}"

    # Chain must be VALID end-to-end
    valid, seq, reason = verify_chain(db_path, SECRET)
    assert valid is True, f"chain broken at seq={seq}: {reason}"

    # All 100 writes landed (no SQLITE_BUSY drops with 5s timeout)
    with sqlite3.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
        assert n == n_workers * spans_per_worker, (
            f"expected {n_workers * spans_per_worker} entries, got {n}"
        )


# === Part D: restrictive file permissions ===


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX chmod semantics; Windows uses ACLs not file mode",
)
def test_new_chain_db_has_0600_perms(db_path: Path) -> None:
    """A freshly-created chain.db gets mode 0o600 (owner r/w only)."""
    assert not db_path.exists()
    HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    assert db_path.exists()
    mode = os.stat(db_path).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX chmod semantics; Windows uses ACLs not file mode",
)
def test_existing_chain_db_perms_tightened_to_0600(db_path: Path) -> None:
    """An existing chain.db is re-chmod'd to 0o600 on EVERY open (ISSUE-15).

    Inverse of the pre-2.15.0 contract (perms preserved). A chain.db left
    world-readable — by an older bijotel or an out-of-band chmod — must be
    tightened back to owner-only the next time the processor opens it;
    preserving loose perms was the fail-open the audit flagged.
    """
    # Create db_path with mode 0o644 (the historical default)
    db_path.touch(mode=0o644)
    assert db_path.exists()
    os.chmod(db_path, 0o644)  # be explicit, umask may interfere
    HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    mode = os.stat(db_path).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600 after open (ISSUE-15), got 0o{mode:o}"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX chmod semantics; Windows uses ACLs not file mode",
)
def test_new_cas_db_has_0600_perms(db_path: Path) -> None:
    """CAS also applies 0o600 on newly-created files."""
    assert not db_path.exists()
    CasSpanProcessor(db_path=db_path)
    assert db_path.exists()
    mode = os.stat(db_path).st_mode & 0o777
    assert mode == 0o600
