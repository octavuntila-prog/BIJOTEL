"""OpenAIAdapter — Provider implementation for OpenAI (F9, v0.4.0).

Second concrete adapter alongside AnthropicAdapter. Validates F7 Provider
Protocol design with a non-Anthropic SDK shape (different method path,
different response attributes).

Lazy client init — adapter is importable without ``openai`` SDK present;
SDK is only resolved at first ``complete()`` or ``client`` property access.

Usage:

    from bijotel.adapters import OpenAIAdapter

    adapter = OpenAIAdapter()
    response = await adapter.complete(
        messages=[{"role": "user", "content": "hi"}],
        model="gpt-4o-mini",
        max_tokens=20,
    )
    print(response.text, response.input_tokens, response.output_tokens)

Or with @trace_genai (F7 integration):

    from bijotel import trace_genai
    from bijotel.adapters import OpenAIAdapter

    adapter = OpenAIAdapter()

    @trace_genai(provider=adapter)
    async def call_gpt(*, model, messages, max_tokens):
        return await adapter.complete(
            messages=messages, model=model, max_tokens=max_tokens
        )

Install requirement: ``pip install bijotel[openai]`` (optional dep).
"""

from __future__ import annotations

from typing import Any

from bijotel.adapters.base import Provider, ProviderResponse
from bijotel.adapters.openai_extractors import (
    extract_openai_request,
    extract_openai_response,
)


class OpenAIAdapter(Provider):
    """OpenAI Provider implementation.

    Args:
        client: Optional ``openai.AsyncOpenAI`` instance. If ``None``,
                lazy-creates on first ``complete()`` call. Pass explicit
                client for testing or custom configuration (base_url,
                timeout, etc.).
    """

    def __init__(self, client: Any | None = None) -> None:
        self._client = client

    @property
    def name(self) -> str:
        return "openai"

    def extract_request_attrs(self, kwargs: dict) -> dict:
        """Delegates to ``extract_openai_request`` (no duplication)."""
        return extract_openai_request(kwargs)

    def extract_response_attrs(self, response: Any) -> dict:
        """Delegates to ``extract_openai_response`` (no duplication)."""
        return extract_openai_response(response)

    @property
    def client(self) -> Any:
        """Lazy-init ``openai.AsyncOpenAI`` client.

        Raises:
            RuntimeError: if ``openai`` package not installed. Suggest
                ``pip install bijotel[openai]`` to user.
        """
        if self._client is None:
            try:
                import openai
            except ImportError as e:
                raise RuntimeError(
                    "openai package not installed. "
                    "Install with: pip install bijotel[openai]"
                ) from e
            self._client = openai.AsyncOpenAI()
        return self._client

    async def complete(
        self,
        *,
        messages: list,
        model: str,
        max_tokens: int,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Call OpenAI ``chat.completions.create`` and normalize to ProviderResponse.

        Extra kwargs (``temperature``, ``tools``, ``top_p``, etc.) pass
        through unchanged.

        Note: OpenAI uses ``max_tokens`` (legacy) or ``max_completion_tokens``
        (GPT-4o / o1 series). This adapter passes ``max_tokens=`` per the
        canonical Provider signature; user can override via kwargs if newer
        param name is required.
        """
        raw = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            **kwargs,
        )

        # Extract primary text from first choice
        text = ""
        if raw.choices and raw.choices[0].message:
            text = raw.choices[0].message.content or ""

        finish_reason = None
        if raw.choices:
            finish_reason = raw.choices[0].finish_reason

        return ProviderResponse(
            text=text,
            model=raw.model,
            input_tokens=raw.usage.prompt_tokens if raw.usage else 0,
            output_tokens=raw.usage.completion_tokens if raw.usage else 0,
            response_id=raw.id,
            finish_reason=finish_reason,
            raw_response=raw,
        )
