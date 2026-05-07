"""Tests pentru bijotel.core.init."""

from __future__ import annotations

import io

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

import bijotel

# Note: reset_otel_global fixture is autouse din tests/conftest.py


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
