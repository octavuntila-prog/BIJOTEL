# Multi-Provider Chains

BIJOTEL is provider-agnostic. The same `chain.db` accepts spans from
Anthropic, OpenAI, xAI, Google, Mistral, DeepSeek, Together, or any
custom provider — as long as the spans carry OpenTelemetry GenAI
attributes (`gen_ai.*`).

`bijotel verify` walks the chain without distinguishing — the HMAC
linkage holds regardless of who emitted each span.

## Anthropic (auto-instrumentation)

The cleanest path. `AnthropicInstrumentor` patches `messages.create()`
at the SDK level — no per-call decorator needed.

```python
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
from bijotel.processors import HmacChainSpanProcessor

provider = TracerProvider()
provider.add_span_processor(HmacChainSpanProcessor(
    db_path="chain.db",
    secret_key=bytes.fromhex(os.environ["BIJOTEL_HMAC_SECRET"]),
))
trace.set_tracer_provider(provider)

AnthropicInstrumentor().instrument()

# Every messages.create() is now in chain.db
from anthropic import Anthropic
client = Anthropic()
client.messages.create(model="claude-haiku-4-5-20251001", ...)
```

This is how production GENA (4 ecosystems, 5,889 entries) is wired,
and how ARA's FastAPI backend integrates BIJOTEL into its lifespan.

## OpenAI-compatible providers

OpenAI SDK works with any OpenAI-compatible API: xAI, Together,
DeepSeek, Mistral, Groq, etc. Use the `@wrap` decorator:

```python
from bijotel import wrap
from openai import OpenAI

xai = OpenAI(
    api_key=os.environ["XAI_API_KEY"],
    base_url="https://api.x.ai/v1",
)

@wrap(provider="xai", model="grok-3-mini")
def call_xai(prompt: str) -> str:
    response = xai.chat.completions.create(
        model="grok-3-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
```

The decorator emits a span with the proper `gen_ai.*` attributes,
which the `HmacChainSpanProcessor` then seals.

## Multi-provider in one app

Just combine the two patterns:

```python
# 1. Set up BIJOTEL (once at startup)
provider = TracerProvider()
provider.add_span_processor(HmacChainSpanProcessor(db_path="chain.db", secret_key=secret))
trace.set_tracer_provider(provider)
AnthropicInstrumentor().instrument()  # captures Anthropic

# 2. Anthropic calls — instrumented automatically
from anthropic import Anthropic
anthro = Anthropic()
anthro.messages.create(model="claude-haiku-4-5-20251001", ...)

# 3. xAI calls — wrap explicitly
@wrap(provider="xai", model="grok-3-mini")
def xai_call(prompt: str):
    return xai.chat.completions.create(...)

xai_call("Hello")
```

Both calls land in `chain.db` under the same HMAC secret. `bijotel
verify` validates them in sequence.

## Cross-provider verification (real example)

GENA's chain.db has Anthropic + xAI spans interleaved since 2026-05-23.
The chain verifies cleanly:

```bash
$ docker exec gena-v3-atelier-1 bijotel verify --db /data/bijotel_chain.db
Chain VALID (5889 entries).
```

The HMAC is provider-blind: it operates on the canonical body of the
span regardless of which `provider.name` attribute it carries.

## Coverage status (v2.0.5)

| Provider | Status | How |
|---|---|---|
| Anthropic | ✅ auto | `AnthropicInstrumentor().instrument()` |
| OpenAI (and compatibles: xAI, Together, DeepSeek, Mistral via SDK, Groq) | ✅ via wrap | `@wrap(provider=..., model=...)` |
| Google Gemini | ⚠️ manual | use `@wrap` or hand-emit `gen_ai.*` spans |
| Cohere | ⚠️ manual | same as Gemini |
| Bedrock / Vertex | ⚠️ manual | same |

Native instrumentors for OpenAI / Google / Cohere are a v2.1 roadmap
item. Today, the wrap decorator covers them with ~3 lines per call site.

## Honest scope

BIJOTEL does not call your LLM. It observes calls your code already
makes. If you're using Langfuse, LangSmith, Helicone, or any other
tracer, BIJOTEL adds a `SpanProcessor` to the same `TracerProvider` —
both observers see every span, but only BIJOTEL seals it
cryptographically.

## Next

- [Quickstart](../getting-started/quickstart.md) for the basic wire-up
- [REST API](../api/rest.md) for `/api/layers` to see provider status
