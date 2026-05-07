"""Tests for default extractors."""

from __future__ import annotations

import json
from types import SimpleNamespace

from bijotel.decorators.extractors import (
    extract_anthropic_request,
    extract_anthropic_response,
)


def test_request_extractor_full() -> None:
    kwargs = {
        "model": "claude-haiku-4-5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
        "tools": [{"name": "tool1", "description": "..."}],
        "system": "You are helpful.",
    }
    result = extract_anthropic_request(kwargs)
    assert result["model"] == "claude-haiku-4-5"
    assert json.loads(result["messages"]) == [{"role": "user", "content": "hi"}]
    assert result["max_tokens"] == 50
    assert json.loads(result["tools"]) == [{"name": "tool1", "description": "..."}]
    assert result["system"] == "You are helpful."


def test_request_extractor_partial() -> None:
    """Missing keys are simply absent in result, not crashing."""
    kwargs = {"model": "x", "messages": []}
    result = extract_anthropic_request(kwargs)
    assert "model" in result
    assert "messages" in result
    assert "max_tokens" not in result
    assert "tools" not in result


def test_request_extractor_empty() -> None:
    """Empty kwargs -> empty dict (no crash)."""
    result = extract_anthropic_request({})
    assert result == {}


def test_response_extractor_anthropic_message() -> None:
    """SimpleNamespace simulates anthropic.types.Message."""
    response = SimpleNamespace(
        model="claude-haiku-4-5",
        id="msg_abc",
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        content=[SimpleNamespace(type="text", text="hello")],
    )
    result = extract_anthropic_response(response)
    assert result["response_model"] == "claude-haiku-4-5"
    assert result["response_id"] == "msg_abc"
    assert result["finish_reason"] == "end_turn"
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 5
    assert "output_messages" in result
    assert "hello" in result["output_messages"]


def test_response_extractor_partial() -> None:
    """Response without usage -> no token attrs (no crash)."""
    response = SimpleNamespace(model="x", id="y")
    result = extract_anthropic_response(response)
    assert result["response_model"] == "x"
    assert "input_tokens" not in result


def test_response_extractor_dict_response() -> None:
    """Response dict (no attributes) -> empty dict (no crash).

    getattr() on dict returns default (None) for any key — dict items
    aren't attributes. Result: empty extraction dict.
    """
    result = extract_anthropic_response({"model": "x"})  # dict, not namespace
    assert result == {}
