"""Tests pentru bijotel.core.init."""

from __future__ import annotations

import io

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.util._once import Once

import bijotel


@pytest.fixture(autouse=True)
def reset_otel_global(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset OTel global TracerProvider between tests (allows re-init).

    OTel's set_tracer_provider() is gated by Once() — only the first call wins.
    We reset the gate per test for isolation.
    """
    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)


def test_init_returns_tracer_provider() -> None:
    """init() returns a configured TracerProvider."""
    output = io.StringIO()
    provider = bijotel.init(service_name="test", output=output)
    assert isinstance(provider, TracerProvider)


def test_init_sets_global_provider() -> None:
    """init() sets the returned provider as global."""
    output = io.StringIO()
    provider = bijotel.init(service_name="test-global", output=output)
    assert trace.get_tracer_provider() is provider


def test_init_emits_span_to_output() -> None:
    """A span created after init() is exported to the output stream."""
    output = io.StringIO()
    bijotel.init(service_name="test-emit", output=output)

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("dummy-span") as span:
        span.set_attribute("test.key", "test.value")

    bijotel.shutdown()

    captured = output.getvalue()
    assert "dummy-span" in captured
    assert "test.key" in captured
