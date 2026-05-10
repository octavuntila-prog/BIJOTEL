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
    rate_limit_calls_per_minute,
)
from bijotel.processors import export_chain, verify_export

__version__ = "0.2.0"

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
    "export_chain",
    "guard",
    "init",
    "model_allowlist",
    "rate_limit_calls_per_minute",
    "shutdown",
    "trace_genai",
    "verify_export",
    "wrap",
]
