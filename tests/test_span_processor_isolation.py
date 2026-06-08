"""IsolatingSpanProcessor — SP-1 hardening (audit 2026-06-08).

A third-party SpanProcessor that raises in a lifecycle hook must not abort the
shared SynchronousMultiSpanProcessor dispatch and skip the HMAC seal. The
wrapper fences such failures; chain sealing always runs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import SpanProcessor, TracerProvider  # noqa: E402

from bijotel.processors import (  # noqa: E402
    HmacChainSpanProcessor,
    IsolatingSpanProcessor,
    verify_chain,
)

SECRET = b"x" * 32


class _RaisingProcessor(SpanProcessor):
    """A hostile/buggy sibling that raises on every lifecycle hook."""

    def on_start(self, span, parent_context=None) -> None:  # noqa: ANN001
        raise RuntimeError("boom-on_start")

    def on_end(self, span) -> None:  # noqa: ANN001
        raise RuntimeError("boom-on_end")

    def _on_ending(self, span) -> None:  # noqa: ANN001
        raise RuntimeError("boom-on_ending")

    def shutdown(self) -> None:
        raise RuntimeError("boom-shutdown")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        raise RuntimeError("boom-force_flush")


def test_isolating_swallows_delegate_exceptions() -> None:
    """Direct calls to the wrapper never propagate the delegate's exceptions."""
    wrapped = IsolatingSpanProcessor(_RaisingProcessor())
    # None of these may raise.
    wrapped.on_start(None)  # type: ignore[arg-type]
    wrapped.on_end(None)  # type: ignore[arg-type]
    wrapped._on_ending(None)  # type: ignore[arg-type]
    assert wrapped.force_flush() is False
    wrapped.shutdown()
    assert wrapped.delegate.__class__ is _RaisingProcessor


def test_isolating_preserves_chain_sealing(tmp_path: Path) -> None:
    """With a raising sibling fenced by the wrapper, the HMAC processor still
    seals the span — the SP-1 failure mode is neutralised."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    # Hostile sibling registered FIRST (would otherwise abort dispatch).
    provider.add_span_processor(IsolatingSpanProcessor(_RaisingProcessor()))
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db, secret_key=SECRET)
    )
    tracer = provider.get_tracer("isolation-test")
    with tracer.start_as_current_span("probe") as span:
        span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        span.set_attribute("gen_ai.usage.input_tokens", 1)
        span.set_attribute("gen_ai.usage.output_tokens", 1)
    provider.shutdown()

    # The HMAC seal ran despite the sibling raising on every hook.
    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM chain").fetchone()[0]
    assert n == 1
    valid, _seq, reason = verify_chain(db, SECRET)
    assert valid, reason
