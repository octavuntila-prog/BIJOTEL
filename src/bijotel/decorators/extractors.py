"""Default request/response extractors for common LLM call patterns.

An extractor takes function kwargs (request) or return value (response)
and produces a dict cu keys care map la gen_ai.* span attributes.

Custom extractors permit user să adapteze la API-uri non-standard
(e.g., wrappers care folosesc pydantic models, dataclasses, etc.).
"""

from __future__ import annotations

import json
from typing import Any


def extract_anthropic_request(kwargs: dict) -> dict:
    """Default extractor pentru Anthropic SDK pattern.

    Anthropic SDK API: client.messages.create(model=..., messages=...,
                                              max_tokens=..., tools=...)

    Returns dict cu keys gata pentru span attributes:
        - model: str
        - messages: JSON-serialized string
        - max_tokens: int (optional)
        - tools: JSON-serialized string (optional)
        - system: str sau JSON (optional)
    """
    out: dict[str, Any] = {}
    if "model" in kwargs:
        out["model"] = kwargs["model"]
    if "messages" in kwargs:
        out["messages"] = json.dumps(kwargs["messages"], ensure_ascii=False)
    if "max_tokens" in kwargs:
        out["max_tokens"] = kwargs["max_tokens"]
    if "tools" in kwargs:
        out["tools"] = json.dumps(kwargs["tools"], ensure_ascii=False)
    if "system" in kwargs:
        out["system"] = (
            kwargs["system"]
            if isinstance(kwargs["system"], str)
            else json.dumps(kwargs["system"], ensure_ascii=False)
        )
    return out


def extract_anthropic_response(response: Any) -> dict:
    """Default extractor pentru Anthropic Message response object.

    Reads attributes via getattr() - works on both real anthropic.types.Message
    și mock objects (SimpleNamespace).

    Returns dict cu:
        - response_model: str (optional)
        - response_id: str (optional)
        - finish_reason: str (optional)
        - input_tokens: int (optional)
        - output_tokens: int (optional)
        - output_messages: JSON string (optional)
    """
    out: dict[str, Any] = {}

    model = getattr(response, "model", None)
    if model:
        out["response_model"] = model

    response_id = getattr(response, "id", None)
    if response_id:
        out["response_id"] = response_id

    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason:
        out["finish_reason"] = stop_reason

    usage = getattr(response, "usage", None)
    if usage is not None:
        in_tokens = getattr(usage, "input_tokens", None)
        out_tokens = getattr(usage, "output_tokens", None)
        if in_tokens is not None:
            out["input_tokens"] = in_tokens
        if out_tokens is not None:
            out["output_tokens"] = out_tokens

    content = getattr(response, "content", None)
    if content is not None:
        # Anthropic content e listă de blocks. Serializăm minimal.
        try:
            blocks = []
            for block in content:
                block_type = getattr(block, "type", "unknown")
                block_text = getattr(block, "text", None)
                blocks.append({"type": block_type, "text": block_text})
            out["output_messages"] = json.dumps(
                [{"role": "assistant", "parts": blocks}], ensure_ascii=False
            )
        except (TypeError, AttributeError):
            pass

    return out


# Aliases pentru clarity în signatures
DEFAULT_REQUEST_EXTRACTOR = extract_anthropic_request
DEFAULT_RESPONSE_EXTRACTOR = extract_anthropic_response
