"""@trace_genai decorator + wrap() runtime alternative.

Auto-detect sync/async based on asyncio.iscoroutinefunction.
Standard OTel span lifecycle (start -> set attributes -> set status -> end).
"""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from bijotel.decorators.extractors import (
    DEFAULT_REQUEST_EXTRACTOR,
    DEFAULT_RESPONSE_EXTRACTOR,
)


def _coerce_to_otel_value(value: Any) -> Any:
    """Coerce list/dict to JSON string (OTel attributes accept only scalars).

    OTel attribute spec rejects dict/list-of-dict (only bool/str/bytes/int/float).
    Custom extractors may forget to JSON-serialize messages/tools — defensively
    coerce here so user gets a usable span attribute, not silent drop.
    """
    if isinstance(value, list | dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def _set_request_attrs(span: trace.Span, request_data: dict) -> None:
    """Map extracted request dict to gen_ai.* span attributes."""
    if "model" in request_data:
        span.set_attribute("gen_ai.request.model", request_data["model"])
    if "messages" in request_data:
        span.set_attribute(
            "gen_ai.input.messages", _coerce_to_otel_value(request_data["messages"])
        )
    if "max_tokens" in request_data:
        span.set_attribute("gen_ai.request.max_tokens", request_data["max_tokens"])
    if "tools" in request_data:
        span.set_attribute(
            "gen_ai.tool.definitions", _coerce_to_otel_value(request_data["tools"])
        )
    if "system" in request_data:
        span.set_attribute(
            "gen_ai.request.system", _coerce_to_otel_value(request_data["system"])
        )


def _set_response_attrs(span: trace.Span, response_data: dict) -> None:
    """Map extracted response dict to gen_ai.* span attributes."""
    if "response_model" in response_data:
        span.set_attribute("gen_ai.response.model", response_data["response_model"])
    if "response_id" in response_data:
        span.set_attribute("gen_ai.response.id", response_data["response_id"])
    if "finish_reason" in response_data:
        span.set_attribute(
            "gen_ai.response.finish_reasons", [response_data["finish_reason"]]
        )
    if "input_tokens" in response_data:
        span.set_attribute(
            "gen_ai.usage.input_tokens", response_data["input_tokens"]
        )
    if "output_tokens" in response_data:
        span.set_attribute(
            "gen_ai.usage.output_tokens", response_data["output_tokens"]
        )
        # Compute total dacă avem ambele
        if "input_tokens" in response_data:
            span.set_attribute(
                "gen_ai.usage.total_tokens",
                response_data["input_tokens"] + response_data["output_tokens"],
            )
    if "output_messages" in response_data:
        span.set_attribute("gen_ai.output.messages", response_data["output_messages"])


def _set_provider_attr(span: trace.Span, provider: str | None) -> None:
    """Set gen_ai.provider.name dacă e furnizat explicit."""
    if provider:
        span.set_attribute("gen_ai.provider.name", provider)


def _set_extra_attrs(span: trace.Span, extra: dict[str, Any] | None) -> None:
    """Set arbitrary additional attributes (e.g. ara.agent_id, ara.org_id)."""
    if not extra:
        return
    for key, value in extra.items():
        span.set_attribute(key, value)


def trace_genai(
    *,
    name: str = "bijotel.llm.call",
    provider: str | None = None,
    request_extractor: Callable[[dict], dict] = DEFAULT_REQUEST_EXTRACTOR,
    response_extractor: Callable[[Any], dict] = DEFAULT_RESPONSE_EXTRACTOR,
    extra_attrs: dict[str, Any] | None = None,
    operation: str = "chat",
) -> Callable:
    """Decorator: trace any function/method care face LLM call.

    Auto-detects sync/async. Emits gen_ai.* compliant spans.

    Args:
        name: Span name (default "bijotel.llm.call").
        provider: Optional provider name (e.g., "anthropic", "openai", "ara").
        request_extractor: Function that extracts request dict from kwargs.
            Defaults to Anthropic SDK style.
        response_extractor: Function that extracts response dict from return value.
            Defaults to Anthropic Message style.
        extra_attrs: Optional dict of additional span attributes (constants).
            Per-call dynamic attrs trebuie set via custom extractor.
        operation: Value pentru gen_ai.operation.name (default "chat").

    Returns:
        Decorator callable.
    """

    def decorator(fn: Callable) -> Callable:
        is_async = asyncio.iscoroutinefunction(fn)

        if is_async:

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                tracer = trace.get_tracer("bijotel.decorators")
                with tracer.start_as_current_span(name, kind=SpanKind.CLIENT) as span:
                    # Pre-call: extract request, set attributes
                    span.set_attribute("gen_ai.operation.name", operation)
                    _set_provider_attr(span, provider)
                    _set_extra_attrs(span, extra_attrs)
                    try:
                        request_data = request_extractor(kwargs)
                        _set_request_attrs(span, request_data)
                    except Exception as e:  # noqa: BLE001
                        span.set_attribute("bijotel.extractor_error", str(e))

                    # Call
                    try:
                        result = await fn(*args, **kwargs)
                    except Exception as e:
                        span.set_status(Status(StatusCode.ERROR, str(e)))
                        span.record_exception(e)
                        raise

                    # Post-call: extract response, set attributes
                    try:
                        response_data = response_extractor(result)
                        _set_response_attrs(span, response_data)
                    except Exception as e:  # noqa: BLE001
                        span.set_attribute("bijotel.extractor_error", str(e))

                    span.set_status(Status(StatusCode.OK))
                    return result

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = trace.get_tracer("bijotel.decorators")
            with tracer.start_as_current_span(name, kind=SpanKind.CLIENT) as span:
                span.set_attribute("gen_ai.operation.name", operation)
                _set_provider_attr(span, provider)
                _set_extra_attrs(span, extra_attrs)
                try:
                    request_data = request_extractor(kwargs)
                    _set_request_attrs(span, request_data)
                except Exception as e:  # noqa: BLE001
                    span.set_attribute("bijotel.extractor_error", str(e))

                try:
                    result = fn(*args, **kwargs)
                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

                try:
                    response_data = response_extractor(result)
                    _set_response_attrs(span, response_data)
                except Exception as e:  # noqa: BLE001
                    span.set_attribute("bijotel.extractor_error", str(e))

                span.set_status(Status(StatusCode.OK))
                return result

        return sync_wrapper

    return decorator


def wrap(
    fn: Callable,
    *,
    name: str = "bijotel.llm.call",
    provider: str | None = None,
    request_extractor: Callable[[dict], dict] = DEFAULT_REQUEST_EXTRACTOR,
    response_extractor: Callable[[Any], dict] = DEFAULT_RESPONSE_EXTRACTOR,
    extra_attrs: dict[str, Any] | None = None,
    operation: str = "chat",
) -> Callable:
    """Runtime equivalent al @trace_genai. No source modification needed.

    Useful pentru cases unde NU poți modifica codul original (third-party libs,
    dynamic dispatch, monkey-patching scenarios).

    Returns:
        Wrapped callable cu același comportament ca @trace_genai applied.
    """
    decorator = trace_genai(
        name=name,
        provider=provider,
        request_extractor=request_extractor,
        response_extractor=response_extractor,
        extra_attrs=extra_attrs,
        operation=operation,
    )
    return decorator(fn)
