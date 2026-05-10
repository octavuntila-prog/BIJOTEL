# BIJOTEL

SpanProcessor plug-ins for OpenTelemetry GenAI applications.

BIJOTEL adds tamper-evidence, content-addressable storage, and in-process policy gating to existing OTel pipelines (OpenLLMetry, custom instrumentations, etc.). It does NOT replace your tracer — it extends it.

**Status:** alpha (F0 skeleton). API will change. Not for production use.

## Architecture

BIJOTEL is a plug-in. You keep your existing OpenTelemetry tracer (e.g., `opentelemetry-instrumentation-anthropic`). BIJOTEL adds three reusable `SpanProcessor`s:

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

from bijotel.processors import (
    HmacChainSpanProcessor,    # F2: tamper-evident audit chain
    CasSpanProcessor,          # F3: content-addressable storage
    PolicyGateSpanProcessor,   # F4: in-process policy gate
)

provider = TracerProvider()
provider.add_span_processor(HmacChainSpanProcessor(secret_key="..."))
provider.add_span_processor(CasSpanProcessor(store_path="./cas.db"))
provider.add_span_processor(PolicyGateSpanProcessor(rules=[...]))
trace.set_tracer_provider(provider)

AnthropicInstrumentor().instrument()  # tracer rămâne upstream
```

## Custom Code Tracing (`@trace_genai`)

For LLM calls outside `instrumentation-anthropic` coverage (custom wrappers,
non-Anthropic providers, multi-provider clients), use the `@trace_genai`
decorator or `bijotel.wrap()` runtime equivalent:

```python
from bijotel import trace_genai

# Anthropic-style API: defaults work
@trace_genai(provider="anthropic")
def call_claude(*, model, messages, max_tokens):
    return client.messages.create(model=model, messages=messages, max_tokens=max_tokens)

# Custom API: provide extractors (e.g. for multi-provider wrappers)
@trace_genai(
    name="ara.llm.call",
    provider="ara",
    request_extractor=lambda kw: {
        "model": kw["cfg"].model_id,
        "messages": kw["messages"],
        "max_tokens": kw["cfg"].max_tokens,
    },
    response_extractor=lambda resp: {
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
    },
    extra_attrs={"ara.deployment": "prod"},  # constants only
)
async def complete(self, *, agent_id, messages, cfg, ...):
    return await self._dispatch(...)
```

Auto-detects sync/async via `asyncio.iscoroutinefunction`. All emitted spans
pass through HmacChain/CAS/Policy processors normally. Exceptions in the
wrapped function set span status to `ERROR` and re-raise. Extractor failures
log to `bijotel.extractor_error` attribute without crashing the call.

`bijotel.wrap(fn, ...)` is the runtime alternative — same behavior, no
source modification needed (third-party libs, dynamic dispatch).

### Note: dual audit when combining `@trace_genai` with `AnthropicInstrumentor`

If you decorate a function that internally calls `client.messages.create()`
while `AnthropicInstrumentor().instrument()` is active, **two spans are
emitted per call**:

- Outer span: from `@trace_genai` (your wrapper boundary)
- Inner span: from `AnthropicInstrumentor` (the SDK call itself)

Both are sealed in the chain. This is intentional — the outer span captures
your application context (e.g. `ara.agent_id`, `ara.org_id`), the inner span
captures the raw SDK request/response. Together they give you full audit
coverage at two granularities.

If you want only one audit layer, choose one approach:
- **Decorator only** (single span per logical call): don't call
  `AnthropicInstrumentor().instrument()`
- **Instrumentation only** (single span per SDK call): don't decorate your
  wrapper

Storage cost of dual audit: ~2× span count. For most workloads this is
trivial; for high-volume production, pick one layer.

## Install

```bash
pip install -e ".[anthropic]"
```

## CLI

After install, the `bijotel` command is available:

```bash
# Verify chain integrity (requires HMAC secret)
export BIJOTEL_HMAC_SECRET=<hex>
bijotel verify --db chain.db

# Inspect a span (by hex span_id or integer seq)
bijotel inspect --db chain.db 1
bijotel inspect --db chain.db abc123def456

# Summary stats (chain + CAS + policy daily state)
bijotel stats --db chain.db

# List spans with filters
bijotel list --db chain.db
bijotel list --db chain.db --blocked
bijotel list --db chain.db --rule cost_per_call_max
bijotel list --db chain.db --model claude-haiku-4-5-20251001
bijotel list --db chain.db --since 2026-05-07 --limit 100
```

`--since` uses calendar date UTC (YYYY-MM-DD, lower bound 00:00:00Z), consistent with `daily_token_budget` rule.

## Validation

End-to-end smoke test on real Anthropic API exercising the full BIJOTEL stack
(HmacChain + CAS + PolicyGate + AnthropicInstrumentor + `@trace_genai`
decorator + all 6 CLI commands):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export BIJOTEL_HMAC_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
python scripts/e2e_smoke.py
```

Cost: ~$0.001 per run (3-4 real Haiku calls; denied calls don't hit network).

The script validates:
- Chain integrity end-to-end (`bijotel verify` returns VALID)
- CAS dedup on identical input (ref_count > 1 for repeated calls)
- Policy gate enforcement (denied calls produce synthetic spans, no SDK call)
- All 6 CLI subcommands return exit 0
- Custom `@trace_genai` decorator works alongside `AnthropicInstrumentor`

## Roadmap

- [x] F0: Skeleton
- [x] F1: End-to-end smallest (init + AnthropicInstrumentor + ConsoleExporter)
- [x] F2: HmacChainSpanProcessor (JCS + SHA-256 + HMAC chain)
- [x] F3: CasSpanProcessor (content-addressable span body storage)
- [x] F4: PolicyGate (3-state Decision + 3 built-in rules + guard decorator)
- [x] F5: `@trace_genai` decorator + `wrap()` runtime (sync+async, custom extractors)
- [x] F6: `bijotel` CLI (verify + inspect + stats + list)
- [x] Validation: `scripts/e2e_smoke.py` (full stack on real Anthropic + CLI verify)
- [ ] F7: Provider protocol abstract + AnthropicAdapter — deferred until multi-provider

## License

MIT
