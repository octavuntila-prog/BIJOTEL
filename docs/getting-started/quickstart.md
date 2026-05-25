# 5-Minute Quickstart

By the end of this you will have:

1. BIJOTEL installed
2. A chain.db sealed against your LLM calls
3. A running `bijotel verify` proving integrity

## 1. Install

```bash
pip install "bijotel[anthropic,api]"
```

## 2. Pick a secret

The HMAC secret protects your chain. **Generate a fresh one**:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
# → e.g.  c1bc9f...64 hex chars total

export BIJOTEL_HMAC_SECRET=<output_from_above>
```

Treat this like a database password. Lose it → you can't verify past
entries. Leak it → an attacker can extend your chain with forged
entries.

## 3. Wire it into your app

The pattern: build a `TracerProvider`, add `HmacChainSpanProcessor`,
call `AnthropicInstrumentor().instrument()`. Every `messages.create()`
now lands in `chain.db`.

```python
import os
from pathlib import Path
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
from bijotel.processors import HmacChainSpanProcessor, CasSpanProcessor

secret = bytes.fromhex(os.environ["BIJOTEL_HMAC_SECRET"])
db = Path("chain.db")

provider = TracerProvider()
provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=secret))
provider.add_span_processor(CasSpanProcessor(db_path=db))
trace.set_tracer_provider(provider)

AnthropicInstrumentor().instrument()
```

## 4. Make a call

```python
from anthropic import Anthropic

client = Anthropic()
response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=50,
    messages=[{"role": "user", "content": "What color is the sky?"}],
)
print(response.content[0].text)
```

That call is now sealed. The span carries `gen_ai.*` attributes
(model, tokens, response), gets canonicalized via RFC 8785 JCS,
hashed with SHA-256, and chained with HMAC-SHA256 to the previous
entry.

## 5. Verify

```bash
bijotel verify --db chain.db
# → Chain VALID (1 entries).
```

Tamper with one byte of `chain.db` and re-run — BIJOTEL pinpoints the
exact `seq` that broke.

## 6. Start the dashboard (optional)

```bash
bijotel serve --port 8080 --db chain.db --dashboard
# → http://localhost:8080
```

You get:

- Chain Explorer (browse entries, search, paginate)
- Policy Decisions page
- Regression monitor
- System status (14 layers)

## Multi-provider in one chain

Need OpenAI / xAI / DeepSeek / Together in the same chain? Use the
`wrap` decorator (see [Multi-Provider](../guides/multi-provider.md)).

## Next

- [First Verification](first-verification.md) — verify the public demo
  chain and see what tampering looks like.
- [Policy Engine](../guides/policy-engine.md) — gate prompts pre-call.
- [Dashboard](../guides/dashboard.md) — full UI tour.
