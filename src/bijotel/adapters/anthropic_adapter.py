"""AnthropicAdapter — Provider implementation for Anthropic Claude (F7).

Wraps ``anthropic.AsyncAnthropic`` client. Reuses F5 extractors
(``extract_anthropic_request`` / ``extract_anthropic_response``) so the
contract methods are zero-duplication delegates.

Lazy client init — adapter is importable without ``anthropic`` installed;
SDK is only resolved on first ``complete()`` call (or first ``client``
property access).

Usage:

    from bijotel.adapters import AnthropicAdapter

    adapter = AnthropicAdapter()
    response = await adapter.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
    )
    print(response.text, response.input_tokens, response.output_tokens)
"""

from __future__ import annotations

import os
from typing import Any

from bijotel.adapters.base import Provider, ProviderResponse
from bijotel.decorators.extractors import (
    extract_anthropic_request,
    extract_anthropic_response,
)


class AnthropicAdapter(Provider):
    """Anthropic Provider implementation.

    Args:
        client: Optional ``anthropic.AsyncAnthropic`` instance. If ``None``,
                lazy-creates on first ``complete()`` call. Pass an explicit
                client for testing or to share state across adapters.
    """

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "anthropic"

    def extract_request_attrs(self, kwargs: dict) -> dict:
        """Reuse F5 ``extract_anthropic_request`` (no duplication)."""
        return extract_anthropic_request(kwargs)

    def extract_response_attrs(self, response: Any) -> dict:
        """Reuse F5 ``extract_anthropic_response`` (no duplication)."""
        return extract_anthropic_response(response)

    @property
    def client(self) -> Any:
        """Lazy-init ``anthropic.AsyncAnthropic`` client.

        Only imports ``anthropic`` package when first accessed — adapter
        remains importable in environments without the SDK installed.
        """
        if self._client is None:
            import anthropic

            _aig_h = (
                {"cf-aig-authorization": f"Bearer {os.environ['CLOUDFLARE_AIG_TOKEN']}"}
                if os.environ.get("CLOUDFLARE_AIG_TOKEN")
                else None
            )
            self._client = anthropic.AsyncAnthropic(
                base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
                default_headers=_aig_h,
            )
        return self._client

    async def complete(
        self,
        *,
        messages: list,
        model: str,
        max_tokens: int,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Call Anthropic SDK and normalize response to ``ProviderResponse``.

        Extra kwargs (``temperature``, ``tools``, ``system``, etc.) pass
        through unchanged to ``messages.create``.
        """
        raw = await self.client.messages.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            **kwargs,
        )

        # Extract text from content blocks (Anthropic returns list of TextBlock,
        # ToolUseBlock, etc.). Concatenate all blocks that have a `.text` attr.
        text = ""
        if raw.content:
            text_blocks = [
                b.text for b in raw.content if hasattr(b, "text")
            ]
            text = "".join(text_blocks)

        return ProviderResponse(
            text=text,
            model=raw.model,
            input_tokens=raw.usage.input_tokens,
            output_tokens=raw.usage.output_tokens,
            response_id=raw.id,
            finish_reason=raw.stop_reason,
            raw_response=raw,
        )
