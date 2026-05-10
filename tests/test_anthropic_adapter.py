"""Tests for AnthropicAdapter (F7)."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bijotel.adapters import AnthropicAdapter
from bijotel.adapters.base import Provider, ProviderResponse
from bijotel.decorators.extractors import (
    extract_anthropic_request,
    extract_anthropic_response,
)


def test_adapter_satisfies_provider_contract() -> None:
    """AnthropicAdapter is a Provider instance."""
    adapter = AnthropicAdapter()
    assert isinstance(adapter, Provider)


def test_adapter_name_returns_anthropic() -> None:
    adapter = AnthropicAdapter()
    assert adapter.name == "anthropic"


def test_adapter_extract_request_delegates_to_f5() -> None:
    """extract_request_attrs returns same output as F5 extract_anthropic_request."""
    adapter = AnthropicAdapter()
    kwargs = {
        "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
    }
    assert adapter.extract_request_attrs(kwargs) == extract_anthropic_request(kwargs)


def test_adapter_extract_response_delegates_to_f5() -> None:
    """extract_response_attrs returns same output as F5 extract_anthropic_response."""
    adapter = AnthropicAdapter()
    response = SimpleNamespace(
        model="claude-haiku-4-5-20251001",
        id="msg_abc",
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        content=[SimpleNamespace(type="text", text="hello")],
    )
    assert adapter.extract_response_attrs(response) == extract_anthropic_response(
        response
    )


def test_adapter_lazy_client_init() -> None:
    """client property is None until first access."""
    adapter = AnthropicAdapter()
    assert adapter._client is None  # noqa: SLF001 (testing internal state)


def test_adapter_accepts_injected_client() -> None:
    """Pass explicit client (e.g. for testing) — no lazy init triggered."""
    fake_client = MagicMock()
    adapter = AnthropicAdapter(client=fake_client)
    assert adapter.client is fake_client


def test_adapter_complete_normalizes_response() -> None:
    """complete() returns ProviderResponse with all fields populated from raw."""
    fake_client = MagicMock()
    raw = SimpleNamespace(
        model="claude-haiku-4-5-20251001",
        id="msg_abc",
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        content=[
            SimpleNamespace(type="text", text="hello "),
            SimpleNamespace(type="text", text="world"),
        ],
    )
    fake_client.messages.create = AsyncMock(return_value=raw)

    adapter = AnthropicAdapter(client=fake_client)
    result = asyncio.run(
        adapter.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-haiku-4-5-20251001",
            max_tokens=50,
        )
    )

    assert isinstance(result, ProviderResponse)
    assert result.text == "hello world"  # joined blocks
    assert result.model == "claude-haiku-4-5-20251001"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.response_id == "msg_abc"
    assert result.finish_reason == "end_turn"
    assert result.raw_response is raw


def test_adapter_complete_handles_empty_content() -> None:
    """complete() with empty/missing content returns text=''."""
    fake_client = MagicMock()
    raw = SimpleNamespace(
        model="m",
        id="x",
        stop_reason=None,
        usage=SimpleNamespace(input_tokens=1, output_tokens=0),
        content=[],
    )
    fake_client.messages.create = AsyncMock(return_value=raw)

    adapter = AnthropicAdapter(client=fake_client)
    result = asyncio.run(
        adapter.complete(messages=[], model="m", max_tokens=10)
    )
    assert result.text == ""


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping smoke",
)
def test_adapter_complete_smoke_real_call() -> None:
    """Real Anthropic call via adapter — ProviderResponse populated end-to-end."""
    adapter = AnthropicAdapter()
    result = asyncio.run(
        adapter.complete(
            messages=[{"role": "user", "content": "Say 'ok' once."}],
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
        )
    )
    assert isinstance(result, ProviderResponse)
    assert "ok" in result.text.lower()
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    assert result.model.startswith("claude-")
    assert result.response_id is not None
