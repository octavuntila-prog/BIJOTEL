"""BIJOTEL: SpanProcessor plug-ins for OpenTelemetry GenAI."""

from bijotel.adapters import AnthropicAdapter, Provider, ProviderResponse
from bijotel.core.init import init, shutdown
from bijotel.decorators import trace_genai, wrap
from bijotel.policy import (
    Decision,
    PolicyDeniedError,
    PolicyEngine,
    cost_per_call_max,
    daily_token_budget,
    guard,
    model_allowlist,
)

__version__ = "0.0.1"

__all__ = [
    "AnthropicAdapter",
    "Decision",
    "PolicyDeniedError",
    "PolicyEngine",
    "Provider",
    "ProviderResponse",
    "__version__",
    "cost_per_call_max",
    "daily_token_budget",
    "guard",
    "init",
    "model_allowlist",
    "shutdown",
    "trace_genai",
    "wrap",
]
