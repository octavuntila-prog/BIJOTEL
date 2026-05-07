"""Shared pytest fixtures pentru toate testele BIJOTEL."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.util._once import Once


@pytest.fixture(autouse=True)
def reset_otel_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset OTel global TracerProvider between tests (allows re-init).

    OTel's set_tracer_provider() is gated by Once() - only the first call wins.
    We reset the gate per test for isolation.
    """
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
