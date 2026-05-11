"""Tests for OpenAIAdapter (F9, v0.4.0).

Validates F7 Provider Protocol design with a second consumer (OpenAI
SDK shape). If these tests pass without F7 base.py changes, the F7
abstraction is proven flexible enough for non-Anthropic providers.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel import trace_genai
from bijotel.adapters import OpenAIAdapter
from bijotel.adapters.base import Provider, ProviderResponse
from bijotel.adapters.openai_extractors import (
    extract_openai_request,
    extract_openai_response,
)
from bijotel.processors import HmacChainSpanProcessor

SECRET = b"x" * 32


# ─── Contract tests ───


def test_openai_adapter_satisfies_provider_contract() -> None:
    """OpenAIAdapter is a Provider instance."""
    adapter = OpenAIAdapter()
    assert isinstance(adapter, Provider)


def test_openai_adapter_name_returns_openai() -> None:
    """name property → 'openai'."""
    assert OpenAIAdapter().name == "openai"


# ─── Request extractor ───


def test_openai_extract_request_basic() -> None:
    """Standard kwargs → expected dict."""
    kwargs = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
    }
    result = extract_openai_request(kwargs)
    assert result["model"] == "gpt-4o-mini"
    assert result["max_tokens"] == 50
    assert json.loads(result["messages"]) == [{"role": "user", "content": "hi"}]


def test_openai_extract_request_max_completion_tokens() -> None:
    """Newer OpenAI param max_completion_tokens recognized."""
    kwargs = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 100,
    }
    result = extract_openai_request(kwargs)
    # Internal contract uses 'max_tokens' key
    assert result["max_tokens"] == 100


def test_openai_extract_request_max_completion_tokens_preferred() -> None:
    """If both present, max_completion_tokens wins (newer)."""
    kwargs = {
        "model": "gpt-4o",
        "max_tokens": 50,
        "max_completion_tokens": 100,
        "messages": [],
    }
    assert extract_openai_request(kwargs)["max_tokens"] == 100


def test_openai_extract_request_extracts_system_from_messages() -> None:
    """OpenAI 'system' role message surfaces as top-level system attr."""
    kwargs = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
        ],
    }
    result = extract_openai_request(kwargs)
    assert result["system"] == "You are helpful."


def test_openai_extract_request_empty_kwargs() -> None:
    """Empty kwargs → empty dict (no crash)."""
    assert extract_openai_request({}) == {}


# ─── Response extractor ───


def _mock_openai_response(
    *,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    content: str = "hello",
    finish_reason: str = "stop",
) -> SimpleNamespace:
    """Build a SimpleNamespace mimicking OpenAI ChatCompletion."""
    return SimpleNamespace(
        id="chatcmpl_test",
        model="gpt-4o-mini",
        choices=[
            SimpleNamespace(
                index=0,
                finish_reason=finish_reason,
                message=SimpleNamespace(role="assistant", content=content),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


def test_openai_extract_response_basic() -> None:
    """Standard response → expected attrs."""
    response = _mock_openai_response()
    result = extract_openai_response(response)
    assert result["response_model"] == "gpt-4o-mini"
    assert result["response_id"] == "chatcmpl_test"
    assert result["finish_reason"] == "stop"
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 5
    assert "output_messages" in result
    assert "hello" in result["output_messages"]


def test_openai_extract_response_handles_partial() -> None:
    """Partial response (missing fields) → graceful, no crash."""
    response = SimpleNamespace(model="gpt-4", id="abc")  # No choices, no usage
    result = extract_openai_response(response)
    assert result["response_model"] == "gpt-4"
    assert result["response_id"] == "abc"
    # Missing fields simply absent in result
    assert "input_tokens" not in result
    assert "output_tokens" not in result


def test_openai_extract_response_dict_input() -> None:
    """Dict response (not Namespace) → empty dict (matches Anthropic
    extractor behavior on dicts)."""
    result = extract_openai_response({"model": "gpt-4"})
    # getattr on dict returns default → no attrs extracted
    assert result == {}


# ─── Adapter delegation ───


def test_openai_adapter_extract_methods_delegate() -> None:
    """Adapter methods produce same output as direct functions."""
    adapter = OpenAIAdapter()
    kwargs = {"model": "gpt-4o", "messages": [{"role": "user", "content": "x"}], "max_tokens": 10}
    assert adapter.extract_request_attrs(kwargs) == extract_openai_request(kwargs)

    response = _mock_openai_response()
    assert adapter.extract_response_attrs(response) == extract_openai_response(response)


# ─── Lazy client init ───


def test_openai_adapter_lazy_client_init() -> None:
    """_client is None until first access."""
    adapter = OpenAIAdapter()
    assert adapter._client is None  # noqa: SLF001 (testing internal)


def test_openai_adapter_accepts_injected_client() -> None:
    """Pass explicit client (for testing) — no lazy init triggered."""
    fake_client = MagicMock()
    adapter = OpenAIAdapter(client=fake_client)
    assert adapter.client is fake_client


def test_openai_adapter_raises_when_openai_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lazy init raises RuntimeError if openai package missing — with
    actionable install hint."""
    import sys

    # Hide openai module to simulate missing package
    monkeypatch.setitem(sys.modules, "openai", None)

    adapter = OpenAIAdapter()
    with pytest.raises(RuntimeError, match="pip install bijotel\\[openai\\]"):
        _ = adapter.client


# ─── complete() normalization ───


def test_openai_adapter_complete_normalizes_response() -> None:
    """complete() returns ProviderResponse with all fields populated."""
    fake_client = MagicMock()
    raw = _mock_openai_response(
        prompt_tokens=15, completion_tokens=8, content="response text", finish_reason="stop"
    )
    fake_client.chat.completions.create = AsyncMock(return_value=raw)

    adapter = OpenAIAdapter(client=fake_client)
    result = asyncio.run(
        adapter.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            max_tokens=50,
        )
    )

    assert isinstance(result, ProviderResponse)
    assert result.text == "response text"
    assert result.model == "gpt-4o-mini"
    assert result.input_tokens == 15
    assert result.output_tokens == 8
    assert result.response_id == "chatcmpl_test"
    assert result.finish_reason == "stop"
    assert result.raw_response is raw


def test_openai_adapter_complete_empty_content() -> None:
    """Response with content=None (rare but valid) → text=''."""
    fake_client = MagicMock()
    raw = _mock_openai_response(content=None)  # type: ignore[arg-type]
    fake_client.chat.completions.create = AsyncMock(return_value=raw)

    adapter = OpenAIAdapter(client=fake_client)
    result = asyncio.run(
        adapter.complete(messages=[], model="gpt-4o", max_tokens=10)
    )
    assert result.text == ""


# ─── Integration with @trace_genai (F7 validation) ───


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


def test_trace_genai_with_openai_adapter_emits_correct_attrs(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """F7 integration: @trace_genai(provider=OpenAIAdapter()) → span has
    gen_ai.provider.name='openai' + request/response attrs from adapter.

    This is the CRITICAL F7 validation test: same decorator, different
    provider, no Provider Protocol changes required.
    """
    adapter = OpenAIAdapter()

    @trace_genai(provider=adapter)
    def my_call(*, model: str, messages: list, max_tokens: int) -> SimpleNamespace:
        return _mock_openai_response(prompt_tokens=25, completion_tokens=12)

    my_call(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], max_tokens=50)

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        assert b'"gen_ai.provider.name":"openai"' in body
        assert b'"gen_ai.request.model":"gpt-4o-mini"' in body
        assert b'"gen_ai.usage.input_tokens":25' in body
        assert b'"gen_ai.usage.output_tokens":12' in body
        assert b'"gen_ai.usage.total_tokens":37' in body


# ─── Smoke: real OpenAI call (skip if no API key) ───


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping real OpenAI smoke",
)
def test_openai_adapter_complete_smoke_real_call() -> None:
    """Real OpenAI call via adapter — ProviderResponse populated end-to-end.

    Cost: ~$0.0001 with gpt-4o-mini (cheapest model, ~20 tokens).
    """
    adapter = OpenAIAdapter()
    result = asyncio.run(
        adapter.complete(
            messages=[{"role": "user", "content": "Say 'ok' once."}],
            model="gpt-4o-mini",
            max_tokens=10,
        )
    )
    assert isinstance(result, ProviderResponse)
    assert "ok" in result.text.lower()
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.model.startswith("gpt-")
    assert result.response_id is not None
