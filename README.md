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

## Roadmap

- [x] F0: Skeleton
- [x] F1: End-to-end smallest (init + AnthropicInstrumentor + ConsoleExporter)
- [x] F2: HmacChainSpanProcessor (JCS + SHA-256 + HMAC chain)
- [x] F3: CasSpanProcessor (content-addressable span body storage)
- [x] F4: PolicyGate (3-state Decision + 3 built-in rules + guard decorator)
- [ ] F5: `@trace_genai` decorator (custom code path) — deferred until concrete use case
- [x] F6: `bijotel` CLI (verify + inspect + stats + list)
- [ ] F7: Provider protocol abstract + AnthropicAdapter — deferred until multi-provider

## License

MIT
