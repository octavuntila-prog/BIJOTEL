# Python API Reference

`bijotel` exposes a small public API (see `bijotel.__all__`). Most users
only need 3-4 symbols.

## Public exports

```python
from bijotel import (
    # Core
    __version__,
    init,
    shutdown,

    # Decorators
    trace_genai,
    wrap,

    # Provider adapters
    AnthropicAdapter,
    OpenAIAdapter,
    Provider,
    ProviderResponse,

    # Policy
    PolicyEngine,
    PolicyDeniedError,
    Decision,
    guard,

    # Built-in rules
    prompt_pattern_deny,
    pii_detection,
    output_length_limit,
    cost_per_call_max,
    daily_token_budget,
    model_allowlist,
    model_version_pin,
    rate_limit_calls_per_minute,
    routing_recommendation,
    energy_budget,
    consensus_requirement,

    # Processors
    HmacChainSpanProcessor,
    CasSpanProcessor,
    FingerprintSpanProcessor,
    EnergySpanProcessor,
    DAGNode,
    MerkleDAG,
    verify_chain,
    verify_export,
    export_chain,

    # Layers
    SemanticFingerprinter,
    DeterministicFingerprinter,
    similarity_search,
    ConsensusVoter,
    ConsensusResult,
    StakesClassifier,
    TaskClassifier,
    ContainmentGuard,
    ContainmentDecision,
    Budget,
    CarbonCalculator,
    EnergyEstimator,
    EnergyTracker,
    EnergySummary,
    ModelRegistry,
    ModelResponse,
    ParetoRouter,
    ASTSafetyChecker,
    ASTViolation,
    ast_safety_check,
    misalignment_check,
    MisalignmentReport,
    Probe,
    ProbeLibrary,
    compute_agreement,

    # Regression
    Anomaly,
    AnomalyMethod,
    DimensionStats,
    RegressionDetector,
    compute_baseline,
)
```

## Core init

```python
import bijotel

# Lightweight tracer setup (F1 schema discovery)
tracer = bijotel.init(service_name="my-service")
bijotel.shutdown()
```

For production wire-up, build the `TracerProvider` yourself and add
processors directly (see [Quickstart](../getting-started/quickstart.md#3-wire-it-into-your-app)).

## Decorators

### `@trace_genai`

Auto-emit a `gen_ai.*` span around any function (sync or async):

```python
from bijotel import trace_genai

@trace_genai(name="my.llm.call", provider="anthropic", operation="chat")
def call_claude(prompt: str) -> str:
    ...
```

Custom request/response extractors:

```python
@trace_genai(
    provider="anthropic",
    request_extractor=lambda kwargs: {
        "gen_ai.request.model": kwargs.get("model"),
        "gen_ai.usage.input_tokens": estimate_tokens(kwargs.get("messages")),
    },
    response_extractor=lambda resp: {
        "gen_ai.response.model": resp.model,
        "gen_ai.usage.output_tokens": resp.usage.output_tokens,
    },
)
def my_call(model, messages): ...
```

### `@wrap`

Simpler decorator for OpenAI-compatible providers:

```python
from bijotel import wrap

@wrap(provider="xai", model="grok-3-mini")
def call_xai(prompt: str): ...
```

## Policy

```python
from bijotel import (
    PolicyEngine,
    prompt_pattern_deny,
    pii_detection,
    PolicyDeniedError,
)

engine = PolicyEngine(rules=[
    prompt_pattern_deny(mode="deny"),
    pii_detection(mode="warn"),
])

decision, warnings = engine.evaluate({
    "messages": [{"role": "user", "content": "..."}],
    "model": "claude-haiku-4-5-20251001",
})

if decision.is_deny:
    raise PolicyDeniedError(decision.reason)
```

The `guard` helper composes engine + an LLM call:

```python
from bijotel import guard, PolicyEngine

@guard(engine=engine)
def call_llm(prompt: str):
    # If engine denies, this function isn't called.
    return anthropic_client.messages.create(...)
```

## Verification

```python
from pathlib import Path
from bijotel.processors import verify_chain, verify_export

# Verify a chain.db
valid, broken_seq, reason = verify_chain(
    Path("chain.db"),
    secret_key=bytes.fromhex("..."),
)
if not valid:
    print(f"Chain broken at seq={broken_seq}: {reason}")

# Verify an exported JSON
valid, reason = verify_export("export.json", secret=bytes.fromhex("..."))
```

## Provider adapters

```python
from bijotel import AnthropicAdapter, OpenAIAdapter

# Anthropic
adapter = AnthropicAdapter()
attrs = adapter.extract_request_attrs(kwargs)  # gen_ai.* dict
response_attrs = adapter.extract_response_attrs(response)

# OpenAI-compatible (xAI, Together, DeepSeek, etc.)
adapter = OpenAIAdapter()
```

## Type hints

The package ships with `py.typed`. Type checkers (mypy, pyright)
honor the public API signatures.

## Stability

Anything in `bijotel.__all__` is stable across patch releases.
Changes are versioned per SemVer and documented in
[`CHANGELOG.md`](../changelog.md).

Internal modules (e.g. `bijotel.processors.canonical`) are not part
of the public API and may change between minor versions.

## Next

- [CLI Reference](cli.md)
- [REST API](rest.md)
