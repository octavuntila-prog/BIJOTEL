# Bootstrap BIJOTEL on a New Ecosystem

This is the operator's runbook for adding BIJOTEL to a fresh
ecosystem (Docker stack, FastAPI app, agent mesh, anything that
makes LLM calls). Target: **~13 minutes end-to-end**.

Two production deployments validate the runbook: GENA (4 main agents,
since 2026-05-10) and ARA (multi-provider backend, since 2026-05-25).
Gotchas at the bottom come from those real deployments.

---

## Prerequisites

- Python ≥ 3.11 in the ecosystem's containers/processes
- Network access to PyPI (or a pre-downloaded wheel)
- A decision: which LLM call sites to instrument

---

## Step 1 — Install (2 min)

```bash
pip install bijotel==2.12.0

# Or, with all optional extras (Anthropic + OpenAI instrumentors,
# fingerprint, AST safety, dashboard API):
pip install "bijotel[all]"
```

The base install is intentionally light. Add extras as needed:

| Extra | Pulls in | When you need it |
|---|---|---|
| `[anthropic]` | `anthropic` + auto-instrumentor | direct Anthropic SDK calls |
| `[openai]` | `openai` SDK | OpenAI / xAI / DeepSeek / Together |
| `[fingerprint]` | `sentence-transformers` | semantic fingerprinting layer |
| `[ast]` | `tree-sitter` + bash grammar | AST safety checker |
| `[api]` | `fastapi` + `uvicorn` | `bijotel serve` dashboard |
| `[mcp]` | `mcp` Python SDK | MCP tool invocation sealing |

---

## Step 2 — Generate HMAC secret (1 min)

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
# → e.g. 5d3a91...
```

Store as `BIJOTEL_HMAC_SECRET` environment variable. **Never** commit
to git. Put it in `.env` (gitignored), Vault, AWS Secrets Manager, or
your secret store of choice.

See [Secret Rotation](secret-rotation.md) for what to do when this
secret needs to change (it eventually will).

---

## Step 3 — Initialize in your app startup (3 min)

Pick the pattern that matches your call surface.

### Pattern A — Anthropic SDK (auto-instrument)

```python
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from bijotel.processors.hmac_chain import HmacChainSpanProcessor
from bijotel.processors.cas import CasSpanProcessor

# Wire OTel + BIJOTEL processors
provider = TracerProvider()
secret = bytes.fromhex(os.environ["BIJOTEL_HMAC_SECRET"])
db_path = "/data/bijotel_chain.db"  # adjust to your persistent volume

provider.add_span_processor(
    HmacChainSpanProcessor(db_path=db_path, secret_key=secret)
)
provider.add_span_processor(CasSpanProcessor(db_path=db_path))
trace.set_tracer_provider(provider)

# Auto-instrument the Anthropic SDK
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
AnthropicInstrumentor().instrument()
```

After this, **every** `client.messages.create(...)` call lands in the
chain. No app-code changes downstream.

### Pattern B — OpenAI-compatible SDKs (xAI, DeepSeek, Together)

Same as Pattern A, but also instrument the OpenAI SDK. `OpenAIInstrumentor`
covers any provider that uses the OpenAI client class with a different
`base_url`:

```python
try:
    from opentelemetry.instrumentation.openai import OpenAIInstrumentor
    OpenAIInstrumentor().instrument()
except ImportError:
    pass  # openai SDK not installed — skip silently
```

### Pattern C — Manual wrapping (any SDK / custom call site)

When auto-instrumentation isn't an option (sync calls, custom HTTP
client, non-Python-SDK provider):

```python
from bijotel import wrap

@wrap(provider="custom", model="my-model")
def call_my_llm(prompt: str) -> str:
    return my_sdk.generate(prompt)

# call_my_llm("...") emits a sealed span on every invocation.
```

### Pattern D — MCP tool invocations (v2.12.0+)

For Model Context Protocol clients:

```python
from bijotel.mcp import MCPInstrumentor
MCPInstrumentor().instrument()
# Every mcp.ClientSession.call_tool(...) now lands in chain.db
```

See the [MCP Invocations guide](../guides/mcp-invocations.md) for details.

---

## Step 4 — Verify chain is sealing (2 min)

After your app has made at least one LLM call:

```bash
bijotel verify --db /data/bijotel_chain.db
# → Chain VALID (N entries).

bijotel stats --db /data/bijotel_chain.db
# → entries, CAS dedup, providers, models, timeline
```

If `verify` returns `INVALID`, something went wrong with the HMAC
secret (mismatched, rotated incorrectly, or corrupted). See [Secret
Rotation](secret-rotation.md).

---

## Step 5 — Start the dashboard (optional, 1 min)

```bash
bijotel serve \
  --dashboard \
  --port 8090 \
  --host 0.0.0.0 \
  --db /data/bijotel_chain.db
```

Browse to `http://your-host:8090/` to get the chain explorer + policy
inspector + regression view. See the [Dashboard guide](../guides/dashboard.md).

For production, run `bijotel serve` under a process supervisor
(systemd, supervisord, or cron-watchdog). Container restart kills the
serve process — add a restart hook.

---

## Step 6 — Add spend protection (optional, 3 min)

L5/L6/L7 spend rules in warn mode are the safest first deploy. They
log warnings instead of blocking calls, so you observe real traffic
against your budgets before enforcing.

```python
from bijotel import (
    PolicyEngine,
    cost_per_call_max,
    daily_token_budget,
    model_allowlist,
)

engine = PolicyEngine([
    cost_per_call_max(usd=0.50, mode="warn"),
    daily_token_budget(
        tokens=2_000_000,
        db_path="/data/bijotel_chain.db",  # same as your chain
        mode="warn",
    ),
    model_allowlist(
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-20250514",
        mode="warn",
    ),
])

# Call before each LLM invocation:
decision, warnings = engine.evaluate({
    "messages": messages,
    "model": model_name,
    "max_tokens": max_tokens,
})
for w in warnings:
    logger.warning(f"BIJOTEL policy [{w.rule}]: {w.reason}")
```

After 1–2 weeks of warn-mode data, flip a rule to `mode="deny"` once
you're confident the threshold doesn't catch legitimate traffic.

---

## Step 7 — Generate Ed25519 keypair for signed exports (optional, 1 min)

```bash
bijotel keygen --output-dir /data/keys/
# → writes bijotel_private.pem + bijotel_public.pem
```

Share `bijotel_public.pem` with downstream auditors. Keep
`bijotel_private.pem` in the same secret store as the HMAC key.

Use the keypair when exporting:

```bash
bijotel export \
  --db /data/bijotel_chain.db \
  --signing-key /data/keys/bijotel_private.pem \
  --out /tmp/chain.signed.json
```

---

## Checklist

| # | Step | Time | Required? |
|---|------|------|-----------|
| 1 | `pip install bijotel` | 2 min | YES |
| 2 | Generate HMAC secret | 1 min | YES |
| 3 | Init in app startup | 3 min | YES |
| 4 | Verify chain sealing | 2 min | YES |
| 5 | Start dashboard | 1 min | Optional |
| 6 | Spend protection (L5/L6/L7) | 3 min | Recommended |
| 7 | Ed25519 keypair | 1 min | Recommended |
| **Total** |  | **~13 min** |  |

---

## Proven deployments

| Ecosystem | First wire | Pattern | Chain entries (2026-05-27) | Notes |
|---|---|---|---|---|
| **GENA** | 2026-05-10 | A (Anthropic) + C (xAI manual wrap) | 6,805+ | 4 main containers, 18 days, 25+ wheel deploys |
| **ARA** | 2026-05-25 | A + B (Anthropic + OpenAI auto) | 218+ | FastAPI lifespan; 5 providers (Anthropic, OpenAI, xAI, DeepSeek, Together) |
| **Gen4** | 2026-05-24 | C (manual wrap on xAI grok-3-mini) | shared GENA chain | Cross-provider verifier inside GENA's chain |

---

## Common gotchas (from real deployments)

### 1. `pip install` inside a running container ≠ permanent

Container recreation wipes runtime `pip install`s. Bake the wheel into
your Docker image at build time, OR keep the install command in your
entrypoint script. We learned this when a `docker compose up --build`
silently downgraded GENA back to a stale baseline.

### 2. `AnthropicInstrumentor` only patches `AsyncAnthropic`

Synchronous `Anthropic()` calls (used in CLI scripts, REPL, ad-hoc
testing) **bypass** the instrumentor entirely. If you mix sync + async
callers, wrap the sync path with Pattern C (`@wrap`).

### 3. `chain.db` must live on a persistent volume

Inside a container, `/tmp/chain.db` is lost on restart. Use
`/data/...` (a named Docker volume) or `/app/data/...` (a mount). Both
GENA and ARA standardized on `/data/bijotel_chain.db` and
`/app/data/bijotel_chain.db` respectively.

### 4. HMAC secret in `.env`, never in code

Add `.env` to `.gitignore` immediately. Load it via `docker-compose`
or `python-dotenv`. The secret leaks the entire integrity guarantee
to anyone who reads your repo.

### 5. `bijotel serve` is a long-lived background process

Container restart kills it. Run under systemd/supervisord, OR add a
cron watchdog that re-launches if the port stops responding. GENA's
watchdog is a 20-line bash script with a 1-minute cron — see
[D1 in the audit logs] for the proven pattern.

### 6. Energy backfill is a one-time job

Energy/CO2 columns are populated by a separate post-call step:

```bash
bijotel energy backfill --db /data/bijotel_chain.db --region eu-north
```

Run once after accumulating ~100 entries; thereafter the
`EnergySpanProcessor` updates new entries automatically.

### 7. PolicyEngine declarative ≠ active

Instantiating `PolicyEngine([...])` in your startup logs reassuring
"PolicyEngine active" messages, but rules **only fire when you call
`engine.evaluate()`**. We made this mistake on ARA — fixed by moving
the engine call site from `lifespan()` into the per-request LLM
client path. Always wire `evaluate()` to the actual call surface, not
just startup.

---

## Ecosystem-specific notes

### Docker Compose stacks (GENA pattern)

- Bake the wheel into the image: `COPY bijotel-*.whl /tmp/ && pip install ...`
- Multi-stage Dockerfile with two targets:
  - `with-bijotel` — for containers that make LLM calls
  - `without-bijotel` — for sidecars, queues, infra (saves ~120MB
    each)
- `docker-compose.yml` picks target per service. See the GENA split
  in `BIJOTEL/docs/operations/docker-image-split.md` (TBD doc; for
  now reference `/opt/substrate-v2/Dockerfile` on GENA).

### FastAPI apps (ARA pattern)

- Wire in `lifespan` async context manager.
- `AnthropicInstrumentor().instrument()` in lifespan startup.
- `chain.db` on a Docker volume (`/app/data/`).
- L5/L6/L7 PolicyEngine: instantiate as module-level singleton in
  the LLM client module, **not** in lifespan (lifespan engine is
  unreachable from per-request paths).

### Agent meshes (multi-process, sharing a chain)

- All processes write to the **same** `chain.db` via SQLite WAL.
- Verified up to 5 concurrent writers in production (R2-B1 test).
- Each process needs its own `TracerProvider` + processors; the
  underlying DB serializes safely via `BEGIN IMMEDIATE`.

---

## When something breaks

| Symptom | Likely cause | Fix |
|---|---|---|
| `Chain INVALID at seq=N` | HMAC secret changed mid-chain | Rotate properly: see [Secret Rotation](secret-rotation.md) |
| `verify` says `0 entries` | App not instrumented OR DB path wrong | Check `BIJOTEL_HMAC_SECRET` is set + DB path is on volume |
| `bijotel serve` 502 | Process died, no watchdog | Add cron watchdog (see GENA D1 pattern) |
| `ModuleNotFoundError: bijotel` | `pip install` didn't persist | Bake into image; check entrypoint |
| Sync calls not sealed | `AnthropicInstrumentor` only covers async | Wrap with `@bijotel.wrap(...)` |

For anything else, open an issue at
[github.com/octavuntila-prog/BIJOTEL/issues](https://github.com/octavuntila-prog/BIJOTEL/issues).
