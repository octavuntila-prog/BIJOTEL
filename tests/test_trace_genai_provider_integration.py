"""Integration tests: @trace_genai with provider= Provider object (F7)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel import trace_genai
from bijotel.adapters import AnthropicAdapter
from bijotel.adapters.base import Provider, ProviderResponse
from bijotel.processors import HmacChainSpanProcessor

SECRET = b"x" * 32


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.db"


@pytest.fixture
def provider_with_chain(db_path: Path) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    )
    trace.set_tracer_provider(provider)
    return provider


def _mock_anthropic_response(input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        model="claude-haiku-4-5",
        id="msg_test",
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=[SimpleNamespace(type="text", text="response")],
    )


def test_trace_genai_with_provider_object_extracts_correctly(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """provider=AnthropicAdapter() → span has gen_ai.provider.name='anthropic'
    + request/response attrs auto-extracted via adapter methods."""
    adapter = AnthropicAdapter()

    @trace_genai(provider=adapter)
    def my_call(*, model: str, messages: list, max_tokens: int) -> SimpleNamespace:
        return _mock_anthropic_response(input_tokens=20, output_tokens=8)

    my_call(
        model="claude-haiku-4-5", messages=[{"role": "user", "content": "hi"}], max_tokens=20
    )

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        assert b'"gen_ai.provider.name":"anthropic"' in body
        assert b'"gen_ai.request.model":"claude-haiku-4-5"' in body
        assert b'"gen_ai.usage.input_tokens":20' in body
        assert b'"gen_ai.usage.output_tokens":8' in body
        assert b'"gen_ai.usage.total_tokens":28' in body


def test_trace_genai_provider_string_backward_compat(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """Legacy F5 path: provider='anthropic' string still works exactly as before."""

    @trace_genai(provider="anthropic")
    def my_call(*, model: str, messages: list, max_tokens: int) -> SimpleNamespace:
        return _mock_anthropic_response()

    my_call(model="claude-haiku-4-5", messages=[{"role": "user"}], max_tokens=20)

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        assert b'"gen_ai.provider.name":"anthropic"' in body
        assert b'"gen_ai.request.model":"claude-haiku-4-5"' in body


def test_trace_genai_no_provider_default(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """provider=None → no gen_ai.provider.name attr in span (default behavior)."""

    @trace_genai()
    def my_call(*, model: str, messages: list, max_tokens: int) -> SimpleNamespace:
        return _mock_anthropic_response()

    my_call(model="claude-haiku-4-5", messages=[{"role": "user"}], max_tokens=20)

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        # No provider name attribute should appear
        assert b'"gen_ai.provider.name"' not in body
        # But default Anthropic extractors still ran (via DEFAULT_REQUEST_EXTRACTOR)
        assert b'"gen_ai.request.model":"claude-haiku-4-5"' in body


def test_explicit_extractors_override_provider_methods(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """provider=adapter + request_extractor=lambda → lambda wins over adapter method."""
    adapter = AnthropicAdapter()

    def custom_request_extractor(kwargs: dict) -> dict:
        return {"model": "custom-override-model"}

    @trace_genai(provider=adapter, request_extractor=custom_request_extractor)
    def my_call(*, model: str, messages: list, max_tokens: int) -> SimpleNamespace:
        return _mock_anthropic_response()

    my_call(model="real-model", messages=[{"role": "user"}], max_tokens=20)

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        # Provider name still from adapter
        assert b'"gen_ai.provider.name":"anthropic"' in body
        # But model from custom extractor (override), NOT from real kwargs/adapter
        assert b'"gen_ai.request.model":"custom-override-model"' in body
        assert b'"gen_ai.request.model":"real-model"' not in body


def test_trace_genai_with_custom_provider_subclass(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """Demonstrate F7 contract: a non-Anthropic Provider works via @trace_genai."""

    class MyProvider(Provider):
        @property
        def name(self) -> str:
            return "custom-provider"

        def extract_request_attrs(self, kwargs: dict) -> dict:
            return {"model": kwargs.get("model_id", "unknown")}

        def extract_response_attrs(self, response: object) -> dict:
            return {
                "input_tokens": getattr(response, "in_tok", 0),
                "output_tokens": getattr(response, "out_tok", 0),
            }

        async def complete(
            self, *, messages: list, model: str, max_tokens: int, **kwargs: object
        ) -> ProviderResponse:
            return ProviderResponse(text="x", model=model, input_tokens=0, output_tokens=0)

    p = MyProvider()

    @trace_genai(provider=p)
    def my_call(*, model_id: str) -> object:
        return SimpleNamespace(in_tok=42, out_tok=11)

    my_call(model_id="custom-model-v1")

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        assert b'"gen_ai.provider.name":"custom-provider"' in body
        assert b'"gen_ai.request.model":"custom-model-v1"' in body
        assert b'"gen_ai.usage.input_tokens":42' in body
        assert b'"gen_ai.usage.output_tokens":11' in body
