"""Core initialization: TracerProvider + ConsoleExporter pentru F1 schema discovery."""

from __future__ import annotations

import json
import sys
from typing import TextIO

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)


def init(
    *,
    service_name: str = "bijotel-f1-discovery",
    output: TextIO | None = None,
    use_simple_processor: bool = True,
) -> TracerProvider:
    """Initialize OTel TracerProvider cu ConsoleSpanExporter.

    F1 scope: schema discovery only. F2+ va adăuga BIJOTEL SpanProcessors.

    Args:
        service_name: Used în resource.service.name span attribute.
        output: Stream pentru ConsoleExporter (default: sys.stdout).
        use_simple_processor: F1 default True (sync, deterministic test order).
            Switch la BatchSpanProcessor pentru production usage.

    Returns:
        Configured TracerProvider (also set as global).
    """
    output = output or sys.stdout

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    exporter = ConsoleSpanExporter(
        out=output,
        formatter=lambda span: json.dumps(json.loads(span.to_json()), indent=2) + "\n",
    )

    processor_cls = SimpleSpanProcessor if use_simple_processor else BatchSpanProcessor
    provider.add_span_processor(processor_cls(exporter))

    trace.set_tracer_provider(provider)
    return provider


def shutdown() -> None:
    """Force flush + shutdown current TracerProvider."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()
