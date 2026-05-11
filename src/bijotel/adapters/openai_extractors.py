"""Request/response extractors for OpenAI SDK (F9).

OpenAI SDK shape differs from Anthropic:

================================  =================================
Anthropic SDK                     OpenAI SDK
================================  =================================
client.messages.create(...)       client.chat.completions.create(...)
response.content[0].text          response.choices[0].message.content
response.usage.input_tokens       response.usage.prompt_tokens
response.usage.output_tokens      response.usage.completion_tokens
response.stop_reason              response.choices[0].finish_reason
================================  =================================

These extractors normalize OpenAI's shape to BIJOTEL's gen_ai.* dict
(matching extract_anthropic_request/response output contract).

Defensive: every attribute access wrapped in try/except for partial
responses or API surface changes.
"""

from __future__ import annotations

import json
from typing import Any


def extract_openai_request(kwargs: dict) -> dict:
    """Extract gen_ai.* request attributes from OpenAI call kwargs.

    Handles both ``max_tokens`` (legacy) and ``max_completion_tokens``
    (newer GPT-4o / o1 models). Returns dict matching BIJOTEL's
    request contract.
    """
    out: dict[str, Any] = {}
    if "model" in kwargs:
        out["model"] = kwargs["model"]
    if "messages" in kwargs:
        out["messages"] = json.dumps(kwargs["messages"], ensure_ascii=False)

    # OpenAI: max_tokens (legacy) OR max_completion_tokens (newer).
    # Prefer max_completion_tokens if both somehow present.
    max_tok = kwargs.get("max_completion_tokens") or kwargs.get("max_tokens")
    if max_tok is not None:
        out["max_tokens"] = max_tok

    if "tools" in kwargs:
        out["tools"] = json.dumps(kwargs["tools"], ensure_ascii=False)

    # OpenAI uses "system" as a message role, not a top-level param.
    # If system message exists in messages, surface it.
    if "messages" in kwargs and isinstance(kwargs["messages"], list):
        for m in kwargs["messages"]:
            if isinstance(m, dict) and m.get("role") == "system":
                content = m.get("content", "")
                out["system"] = content if isinstance(content, str) else json.dumps(
                    content, ensure_ascii=False
                )
                break

    return out


def extract_openai_response(response: Any) -> dict:
    """Extract gen_ai.* response attributes from OpenAI ChatCompletion.

    Returns dict matching BIJOTEL's response contract. Defensive on
    every field — partial responses or SDK changes return graceful
    defaults rather than crashing.
    """
    out: dict[str, Any] = {}

    model = getattr(response, "model", None)
    if model:
        out["response_model"] = model

    response_id = getattr(response, "id", None)
    if response_id:
        out["response_id"] = response_id

    # Finish reason lives on the choice, not the response root
    choices = getattr(response, "choices", None) or []
    if choices:
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            out["finish_reason"] = finish_reason

        # Output messages — OpenAI structure
        message = getattr(choice, "message", None)
        if message is not None:
            try:
                role = getattr(message, "role", "assistant")
                content = getattr(message, "content", None) or ""
                out["output_messages"] = json.dumps(
                    [{"role": role, "parts": [{"type": "text", "text": content}]}],
                    ensure_ascii=False,
                )
            except (TypeError, AttributeError):
                pass

    # Token usage — OpenAI uses prompt_tokens / completion_tokens
    usage = getattr(response, "usage", None)
    if usage is not None:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        if prompt_tokens is not None:
            out["input_tokens"] = prompt_tokens
        if completion_tokens is not None:
            out["output_tokens"] = completion_tokens

    return out
