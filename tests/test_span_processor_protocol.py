"""Protocol test: every bijotel SpanProcessor survives a REAL span lifecycle.

Regression guard for the 2026-06-05 GENA incident. ``EnergySpanProcessor`` was
duck-typed (not an ``opentelemetry.sdk.trace.SpanProcessor`` subclass) and so
lacked ``_on_ending`` — a method the OTel SDK (>=1.42) calls on span end via
``span.end() -> processor._on_ending(span)``. Wiring it into a live
TracerProvider broke chain sealing on GENA (every span end raised
``AttributeError``). The per-processor unit tests only ever called ``on_end()``
directly, so they never exercised ``_on_ending`` — 979 green tests missed it.

This test drives a real TracerProvider start->end cycle (which invokes the
full SpanProcessor protocol, including ``_on_ending``), parametrized over ALL
bijotel processors, so the entire class of protocol-drift is blocked — not
just this one method.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from bijotel.layers.energy import EnergySpanProcessor, EnergyTracker  # noqa: E402
from bijotel.layers.fingerprint import (  # noqa: E402
    DeterministicFingerprinter,
    FingerprintSpanProcessor,
)
from bijotel.processors import CasSpanProcessor, HmacChainSpanProcessor  # noqa: E402


def _hmac(tmp: Path):
    return HmacChainSpanProcessor(db_path=tmp / "chain.db", secret_key=b"x" * 32)


def _cas(tmp: Path):
    return CasSpanProcessor(db_path=tmp / "chain.db")


def _fingerprint(tmp: Path):
    return FingerprintSpanProcessor(
        db_path=tmp / "fp.db", fingerprinter=DeterministicFingerprinter()
    )


def _energy(tmp: Path):
    return EnergySpanProcessor(EnergyTracker(tmp / "chain.db"))


@pytest.mark.parametrize(
    "factory",
    [_hmac, _cas, _fingerprint, _energy],
    ids=["hmac", "cas", "fingerprint", "energy"],
)
def test_span_processor_survives_real_lifecycle(tmp_path: Path, factory) -> None:
    """A real start->end span cycle must complete without raising.

    Exercises on_start, on_end, AND _on_ending via the OTel SDK — the protocol
    surface a live TracerProvider actually drives, which on_end()-only unit
    tests miss. A duck-typed processor missing any protocol method fails here.
    """
    provider = TracerProvider()
    provider.add_span_processor(factory(tmp_path))
    tracer = provider.get_tracer("protocol-test")
    with tracer.start_as_current_span("probe") as span:
        span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        span.set_attribute("gen_ai.usage.input_tokens", 1)
        span.set_attribute("gen_ai.usage.output_tokens", 1)
    # span.end() fired on context exit -> _on_ending was invoked. shutdown()
    # flushes any batch. Reaching here without AttributeError == protocol OK.
    provider.shutdown()
