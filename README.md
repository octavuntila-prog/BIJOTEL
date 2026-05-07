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

## Roadmap

- [x] F0: Skeleton
- [ ] F1: End-to-end smallest (init + AnthropicInstrumentor + ConsoleExporter)
- [ ] F2: HmacChainSpanProcessor (JCS + SHA-256 + HMAC chain)
- [ ] F3: CasSpanProcessor (content-addressable span body storage)
- [ ] F4: PolicyGateSpanProcessor (pre-call ALLOW/DENY callback)
- [ ] F5: `@trace_genai` decorator (custom code path)
- [ ] F6: `bijotel verify` + `bijotel inspect` CLI
- [ ] F7: Provider protocol abstract + AnthropicAdapter

## License

MIT
