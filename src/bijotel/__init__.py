"""BIJOTEL: SpanProcessor plug-ins for OpenTelemetry GenAI."""

from bijotel.adapters import (
    AnthropicAdapter,
    OpenAIAdapter,
    Provider,
    ProviderResponse,
)
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
    prompt_pattern_deny,
    rate_limit_calls_per_minute,
)
from bijotel.processors import export_chain, verify_export
from bijotel.regression import (
    Anomaly,
    AnomalyMethod,
    DimensionStats,
    RegressionDetector,
    compute_baseline,
)

__version__ = "0.6.0"

__all__ = [
    "Anomaly",
    "AnomalyMethod",
    "AnthropicAdapter",
    "Decision",
    "DimensionStats",
    "OpenAIAdapter",
    "PolicyDeniedError",
    "PolicyEngine",
    "Provider",
    "ProviderResponse",
    "RegressionDetector",
    "__version__",
    "compute_baseline",
    "cost_per_call_max",
    "daily_token_budget",
    "export_chain",
    "guard",
    "init",
    "model_allowlist",
    "prompt_pattern_deny",
    "rate_limit_calls_per_minute",
    "shutdown",
    "trace_genai",
    "verify_export",
    "wrap",
]
