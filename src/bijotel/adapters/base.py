"""Provider Protocol — base contract for LLM provider adapters (F7).

Provider unifies LLM provider integration. Implementations expose:
    - ``name`` property → ``gen_ai.provider.name``
    - ``extract_request_attrs`` → maps call kwargs to gen_ai.* request attrs
    - ``extract_response_attrs`` → maps response object to gen_ai.* response attrs
    - ``complete`` async method → canonical signature returning ``ProviderResponse``

The same callable semantics as F5 extractors (``decorators/extractors.py``)
so adapters can reuse F5 helpers (e.g. ``AnthropicAdapter`` delegates to
``extract_anthropic_request`` / ``extract_anthropic_response``).

Usage with ``@trace_genai``:

    from bijotel import trace_genai
    from bijotel.adapters import AnthropicAdapter

    adapter = AnthropicAdapter()

    @trace_genai(provider=adapter)
    async def my_call(*, model, messages, max_tokens):
        return await adapter.complete(
            messages=messages, model=model, max_tokens=max_tokens
        )
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderResponse:
    """Normalized response across providers.

    Concrete adapters convert provider-specific response objects to this.
    Maps directly to ``gen_ai.*`` span attributes.

    Args:
        text: Primary text output (joined content blocks if multi-part).
        model: Response model identifier (may differ from request model).
        input_tokens: Tokens consumed in input.
        output_tokens: Tokens generated in output.
        response_id: Provider-assigned response ID (e.g. ``msg_01...``).
        finish_reason: Why generation stopped (``stop`` / ``max_tokens`` / etc.).
        raw_response: Original provider response object (escape hatch).
        extra_attrs: Provider-specific extras (cache tokens, tool_use, etc.).
    """

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    response_id: str | None = None
    finish_reason: str | None = None
    raw_response: Any = None
    extra_attrs: dict[str, Any] = field(default_factory=dict)


class Provider(ABC):
    """Provider Protocol — implementations integrate LLM providers.

    Subclass + implement four members. ``AnthropicAdapter`` is the reference
    implementation; future ``OpenAIAdapter`` / ``GeminiAdapter`` follow same
    contract.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for ``gen_ai.provider.name`` attribute.

        Conventionally lowercase short slug (``anthropic``, ``openai``,
        ``gemini``, ``bedrock``).
        """

    @abstractmethod
    def extract_request_attrs(self, kwargs: dict) -> dict:
        """Extract request attributes from call kwargs.

        Returns dict mapping to ``gen_ai.*`` request attributes:
            - ``model``: request model name
            - ``messages``: JSON-serialized messages list
            - ``max_tokens``: max output tokens (optional)
            - ``tools``: JSON-serialized tools (optional)
            - ``system``: system prompt (optional)

        Compatible with F5 ``request_extractor`` callable signature so adapters
        can be passed as the ``provider=`` parameter to ``@trace_genai``.
        """

    @abstractmethod
    def extract_response_attrs(self, response: Any) -> dict:
        """Extract response attributes from provider response.

        Returns dict mapping to ``gen_ai.*`` response attributes:
            - ``response_model``
            - ``response_id``
            - ``finish_reason``
            - ``input_tokens``
            - ``output_tokens``
            - ``output_messages`` (JSON-serialized)

        Compatible with F5 ``response_extractor`` callable signature.
        """

    @abstractmethod
    async def complete(
        self,
        *,
        messages: list,
        model: str,
        max_tokens: int,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Canonical completion call.

        Concrete implementations call provider SDK and normalize result to
        ``ProviderResponse``. The ``**kwargs`` permits provider-specific extras
        (e.g. ``temperature``, ``tools``, ``system``) to pass through.
        """
