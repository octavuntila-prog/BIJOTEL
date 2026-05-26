# Multi-provider instrumentation

BIJOTEL is provider-agnostic: it seals every OpenTelemetry GenAI span
that lands in its TracerProvider, regardless of which LLM SDK produced
it. The shape of the integration depends on how many SDKs your
application imports.

This page documents the deployment pattern, with the ARA (AI Research
Agency) production deploy as the worked example.

---

## TL;DR

For every LLM SDK in use, wire its OTel instrumentor in your
application's startup (FastAPI lifespan, Django `AppConfig.ready`,
Flask `before_first_request`, etc.) **before** the first LLM call.
The instrumentor patches the SDK at the import boundary; every call
site downstream then emits spans automatically.

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
from opentelemetry.instrumentation.openai import OpenAIInstrumentor
from bijotel.processors import HmacChainSpanProcessor, CasSpanProcessor

provider = TracerProvider()
provider.add_span_processor(HmacChainSpanProcessor(
    db_path="chain.db",
    secret_key=bytes.fromhex(os.environ["BIJOTEL_HMAC_SECRET"]),
))
provider.add_span_processor(CasSpanProcessor(db_path="chain.db"))
trace.set_tracer_provider(provider)

AnthropicInstrumentor().instrument()
OpenAIInstrumentor().instrument()
# add more instrumentors per SDK you import
```

After this, every `client.messages.create()` (Anthropic) and
`client.chat.completions.create()` (OpenAI, including OpenAI-compatible
backends like xAI / DeepSeek / Together) is sealed into the chain.

---

## SDK-to-instrumentor coverage matrix

| Provider | Python SDK | OTel instrumentor | One-instrumentor coverage |
|---|---|---|---|
| Anthropic | `anthropic` | `opentelemetry-instrumentation-anthropic` | — |
| OpenAI | `openai` | `opentelemetry-instrumentation-openai` | OpenAI + xAI + DeepSeek + Together + Fireworks + Groq + any other OpenAI-compatible service that uses `openai.OpenAI(base_url=…)` |
| Google Gemini (`google-genai` SDK) | `google-genai` | **No OTel instrumentor on PyPI as of 2026-05-26.** The older `opentelemetry-instrumentation-vertexai` is for the legacy `vertexai` SDK, not `google-genai`. | Track upstream or wrap manually (see "Gap workarounds" below). |
| Mistral | `mistralai` | `opentelemetry-instrumentation-mistral` (third-party, not officially supported) | Same install pattern; not exercised in this matrix. |
| AWS Bedrock | `boto3` | `opentelemetry-instrumentation-botocore` | Captures Bedrock calls inside the broader botocore span. |
| Cohere | `cohere` | `opentelemetry-instrumentation-cohere` | — |

The big leverage point: **OpenAIInstrumentor patches the `openai`
SDK once, and every consumer of `openai.OpenAI` / `openai.AsyncOpenAI`
gets spans automatically — regardless of `base_url`**. That covers the
majority of "OpenAI-compatible" providers in the wild without extra
config.

---

## Worked example: ARA (AI Research Agency)

ARA is a 7-provider research platform with one LLM call site
(`backend/llm/client.py::LLMClient.complete()`) and five active
provider methods (`_call_anthropic`, `_call_openai_compat`,
`_call_google`, `_call_mistral`, plus the shared dispatch).
`_call_openai_compat` handles OpenAI, xAI, DeepSeek, and Together via
one `openai.AsyncOpenAI` client with different `base_url` overrides.

### Before (v2.0.x deploy, 2026-05-25)

ARA wired only `AnthropicInstrumentor` in the FastAPI lifespan. Result:
153 sealed chain entries over 24 hours, 100% anthropic. Other 5
providers were running ungated outside the chain.

### After (v2.1.0 deploy, 2026-05-26)

Added one block to the lifespan after the anthropic init:

```python
try:
    from opentelemetry.instrumentation.openai import OpenAIInstrumentor
    OpenAIInstrumentor().instrument()
    providers_wired.extend(["openai", "xai", "deepseek", "together"])
except ImportError:
    logger.warning("opentelemetry-instrumentation-openai not installed")
```

Added one line to `requirements.txt`:

```
opentelemetry-instrumentation-openai==0.60.0
```

After restart, the lifespan log reads:

```
BIJOTEL: chain=/app/data/bijotel_chain.db,
  providers instrumented=anthropic,openai,xai,deepseek,together
  (google/mistral: see comments in main.py lifespan)
```

End-to-end test from inside the container: real OpenAI + xAI calls
sealed correctly, producing chain entries with the expected
`gen_ai.request.model` and token counts. `bijotel verify` reported
`Chain VALID (155 entries)` post-deploy.

### Gap workarounds in this build

- **Google Gemini (`google-genai`)** — no PyPI instrumentor. ARA's
  `_call_google()` calls remain outside the chain in this deploy.
  Options:
  - Wait for an upstream `opentelemetry-instrumentation-google-genai`
    package.
  - Wrap the `_call_google` method body with `tracer.start_as_current_span("google.chat", ...)` and set the standard `gen_ai.*` attributes
    manually — BIJOTEL's HmacChainSpanProcessor will pick it up.
  - Use `bijotel.wrap()` on the SDK call site for a quick manual
    bridge.
- **Mistral (`mistralai`)** — the SDK is currently absent from the
  build (PyPI quarantine). ARA's `MISTRAL_SDK_AVAILABLE` guard
  short-circuits `_call_mistral`; no calls flow through it. Nothing to
  instrument until the SDK returns.

---

## How to verify in your own deploy

```bash
# 1. Snapshot chain count
sqlite3 chain.db "SELECT COUNT(*) FROM chain"

# 2. Make a real call through each instrumented SDK

# 3. Re-count + check provider distribution
sqlite3 chain.db "
  SELECT json_extract(canonical_body,
                      '$.attributes.\"gen_ai.provider.name\"') AS provider,
         COUNT(*) AS calls
  FROM chain
  GROUP BY provider
  ORDER BY calls DESC
"

# 4. Confirm chain integrity
bijotel verify --db chain.db
```

Provider names you'll see in `gen_ai.provider.name`:

| Actual API call | Reported provider |
|---|---|
| Anthropic | `anthropic` |
| OpenAI (`api.openai.com`) | `openai` |
| xAI (`api.x.ai`, OpenAI SDK + base_url) | `openai` (SDK-reported) |
| DeepSeek (OpenAI SDK + base_url) | `openai` |
| Together (OpenAI SDK + base_url) | `openai` |

`gen_ai.provider.name` reflects the **SDK** that emitted the span, not
the actual upstream API. To distinguish OpenAI-proper from xAI, look at
`gen_ai.request.model` — model names disambiguate (`gpt-4o-mini` vs
`grok-3-mini` vs `deepseek-chat` vs `meta-llama/Llama-3-…`).

This is a documented behaviour of `opentelemetry-instrumentation-openai`,
not a BIJOTEL quirk. If you need to log the "logical" provider for
billing or routing analytics, set a span attribute yourself in your
client wrapper before the SDK call.

---

## Rollback

If a multi-provider rollout breaks the lifespan and the backend won't
start:

```bash
# Restore previous main.py and requirements.txt:
cp /path/to/main.py.pre-multi-provider.bak /path/to/main.py
cp /path/to/requirements.txt.pre-multi-provider.bak /path/to/requirements.txt

# Restart:
docker compose restart backend
# or for a full rebuild:
docker compose build backend && docker compose up -d backend
```

BIJOTEL's lifespan block is wrapped in a `try/except` that logs the
error and continues — so a broken instrumentor import won't crash the
host application, just produce a degraded chain (missing one
provider). The fail-soft posture is intentional.
