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

## Provider Adapters (F7)

Provider Protocol unifies LLM provider integration. Adapters implement
contract methods, enabling clean `@trace_genai` integration via
`provider=adapter` shorthand:

```python
from bijotel import trace_genai
from bijotel.adapters import AnthropicAdapter

adapter = AnthropicAdapter()

@trace_genai(provider=adapter)
async def my_call(*, model, messages, max_tokens):
    return await adapter.complete(
        messages=messages, model=model, max_tokens=max_tokens
    )
```

The decorator auto-extracts:
- `gen_ai.provider.name` from `adapter.name`
- Request attrs from `adapter.extract_request_attrs()`
- Response attrs from `adapter.extract_response_attrs()`

Explicit `request_extractor=` / `response_extractor=` always override
adapter-supplied methods (escape hatch preserved).

Calling the adapter directly returns a normalized `ProviderResponse`:

```python
response = await adapter.complete(
    messages=[{"role": "user", "content": "hi"}],
    model="claude-haiku-4-5-20251001",
    max_tokens=20,
)
print(response.text, response.input_tokens, response.output_tokens)
```

**Available adapters:**
- `AnthropicAdapter` — Anthropic Claude (uses `anthropic.AsyncAnthropic`). Install: `pip install bijotel[anthropic]`.
- `OpenAIAdapter` — OpenAI GPT (uses `openai.AsyncOpenAI`). Install: `pip install bijotel[openai]`.

```python
from bijotel import trace_genai
from bijotel.adapters import OpenAIAdapter

adapter = OpenAIAdapter()

@trace_genai(provider=adapter)
async def call_gpt(*, model, messages, max_tokens):
    return await adapter.complete(
        messages=messages, model=model, max_tokens=max_tokens
    )

# Direct call:
response = await adapter.complete(
    messages=[{"role": "user", "content": "hi"}],
    model="gpt-4o-mini",
    max_tokens=20,
)
```

Same `Provider` Protocol, same `ProviderResponse` shape — only the SDK underneath differs. F7 validated empirical with two consumers (Anthropic + OpenAI).

**Adding new providers** — subclass `Provider`:

```python
from bijotel.adapters import Provider, ProviderResponse

class OpenAIAdapter(Provider):
    @property
    def name(self) -> str:
        return "openai"

    def extract_request_attrs(self, kwargs): ...
    def extract_response_attrs(self, response): ...

    async def complete(self, *, messages, model, max_tokens, **kwargs):
        raw = await self.client.chat.completions.create(...)
        return ProviderResponse(
            text=raw.choices[0].message.content,
            model=raw.model,
            input_tokens=raw.usage.prompt_tokens,
            output_tokens=raw.usage.completion_tokens,
            response_id=raw.id,
            finish_reason=raw.choices[0].finish_reason,
            raw_response=raw,
        )
```

Backward-compatible: passing `provider="anthropic"` (string) still works
exactly as in F5 — Provider object is opt-in.

## Policy Gate

The `PolicyEngine` evaluates pre-call rules against request payload (model, messages, max_tokens, …) and returns a `Decision` (allow / warn / deny). Use the `guard` decorator for the typical "wrap an LLM call" pattern, or call `PolicyEngine` directly for custom integration.

### `PolicyEngine` direct usage

```python
from bijotel import PolicyEngine, cost_per_call_max, model_allowlist

engine = PolicyEngine(rules=[
    cost_per_call_max(usd=0.50),
    model_allowlist("claude-haiku-4-5", "claude-sonnet-4-20250514"),
])

request = {"model": "claude-haiku-4-5", "messages": [...], "max_tokens": 100}
decision = engine.evaluate(request)

if decision.is_deny:
    print(f"Blocked by {decision.rule}: {decision.reason}")
elif decision.is_warn:
    print(f"Warning from {decision.rule}: {decision.reason}")  # call still proceeds
else:
    print("Allowed")
```

`engine.evaluate()` short-circuits on first deny. Warnings are collected and attached as `bijotel.policy.warning` attributes on emitted spans. See `Decision` and `State` classes in `bijotel.policy.decision`.

### `model_allowlist`

Restrict which models can be called via your wrapper. Useful for cost control + audit.

```python
from bijotel import model_allowlist

# Deny if model not in list
rule = model_allowlist("claude-haiku-4-5", "claude-sonnet-4-20250514", mode="deny")

# Warn-only mode (audit + proceed)
rule_audit = model_allowlist("claude-haiku-4-5", mode="warn")
```

### `prompt_pattern_deny` (F11)

Block prompts matching jailbreak / prompt-injection regex patterns *before* the SDK call is made. Five attack categories covered out of the box: instruction override (`"ignore previous instructions"`), system prompt extraction (`"reveal your system prompt"`), role override (`"you are now a different AI"`), jailbreak framing (`"DAN mode"`, `"developer mode"`), encoding bypass (`base64:`, `rot13`). Defaults are case-insensitive and applied via `re.search`.

```python
from bijotel import prompt_pattern_deny

# Defaults only (DEFAULT_JAILBREAK_PATTERNS, ~15 patterns, 5 categories)
rule = prompt_pattern_deny()

# Custom patterns appended to defaults (defaults checked first)
rule = prompt_pattern_deny(
    patterns=[r"my_company_secret", r"\bAPI[_-]KEY\b"],
)

# Custom patterns only — defaults disabled
rule = prompt_pattern_deny(
    patterns=[r"sensitive_term"], use_defaults=False
)

# Warn mode — audit but allow (recommended for first deployment)
rule_audit = prompt_pattern_deny(mode="warn")
```

Handles both Anthropic SDK (`messages=[{"role": "user", "content": "..."}]`) and Anthropic multipart format (`content=[{"type": "text", "text": "..."}]`), plus OpenAI-style messages — extracts and concatenates text content from all roles before matching.

Suggested rollout: deploy in `mode="warn"` first to surface false positives via `bijotel.policy.warning` span attributes, review for ~1 week, then flip to `mode="deny"`. False positives are easier to diagnose than false negatives in this domain.

Pattern catalog adapted from substrate-guard's `agent_safety.rego` `dangerous_patterns` concept (separate project, read-only access). The substrate-guard version targets filesystem / network / shell actions; this BIJOTEL adaptation targets LLM prompts (instruction overrides, system-prompt extraction, role overrides, jailbreak framings, encoding bypass).

### `PolicyDeniedError`

Raised by `guard()` decorator when a rule returns `Decision.deny`. Catch it in your application code to surface a useful message:

```python
from bijotel import guard, PolicyDeniedError, cost_per_call_max

@guard(rules=[cost_per_call_max(usd=0.10)])
def call_llm(*, model, messages, max_tokens):
    return client.messages.create(model=model, messages=messages, max_tokens=max_tokens)

try:
    response = call_llm(model="claude-opus-4-7", messages=[...], max_tokens=4000)
except PolicyDeniedError as e:
    print(f"Policy denied: rule={e.rule!r}, reason={e.reason!r}")
    # → returns to user instead of leaking expensive call
```

## Chain export — programmatic API

CLI is the typical use, but `export_chain` and `verify_export` are exposed as public functions for programmatic integration (e.g. scheduled audit-trail uploads, CI verification jobs):

```python
from pathlib import Path
from bijotel import export_chain, verify_export

secret = bytes.fromhex("<your hex secret>")  # min 16 bytes

# Export
out = export_chain(
    db_path=Path("/data/bijotel_chain.db"),
    output_path=Path("/var/audit/audit_2026-05-10.json"),
    secret_key=secret,
)
# → "/var/audit/audit_2026-05-10.json"

# Verify (auditor side, only needs secret + JSON file)
valid, reason = verify_export(out, secret)
if not valid:
    raise RuntimeError(f"Audit trail tampered: {reason}")
```

Schema: `bijotel-chain-v1`. Per-entry HMAC + file-level `chain_signature`. Integrity verifiable with shared secret only — no SQLite access required.

## Regression Detection (F12, Bijuteria #16)

Detect drift in token usage / cost over time using z-score + IQR methods on
the BIJOTEL chain.db. Empirically motivated by patterns observed during
GENA deployment (T+2h checkpoint revealed bimodal quality distributions
and dimension-specific bottlenecks worth monitoring temporally).

### Programmatic API

```python
from bijotel import RegressionDetector, AnomalyMethod

detector = RegressionDetector(
    db_path="chain.db",
    baseline_window=100,        # Use last 100 spans as baseline
    z_threshold=3.0,            # Flag values > 3σ from mean
    iqr_multiplier=1.5,         # Tukey-style IQR outlier
    method=AnomalyMethod.BOTH,  # Require BOTH methods to flag (low FP)
)

# Single dimension
anomalies = detector.detect("input_tokens")
for a in anomalies:
    print(f"  seq={a.seq} value={a.value} z={a.z_score:.2f} severity={a.severity}")

# All 3 dimensions (input_tokens, output_tokens, cost)
results = detector.detect_all_dimensions(filter_model="claude-haiku-4-5-20251001")
```

### CLI usage

```bash
# Scan all 3 dimensions on entire chain (default: last 50 spans vs prior 100)
bijotel regression --db chain.db

# Single dimension, specific model
bijotel regression --db chain.db --dimension cost --model claude-sonnet-4-20250514

# Custom baseline window + sensitivity
bijotel regression --db chain.db --window 200 --z-threshold 2.5
```

Exit codes: `0` no anomalies, `1` anomalies detected, `2` invalid args.

### Detection methods

- **z-score** (parametric): `z = (value - baseline.mean) / baseline.stdev`. Fast for Gaussian-like signals (most token counts when calls are similar).
- **IQR** (non-parametric, Tukey): flag if `value < p25 - k·iqr` OR `value > p75 + k·iqr`. Robust to heavy-tailed distributions (cost can spike).
- **`AnomalyMethod.BOTH`** (default): flags only when BOTH agree → minimizes false positives. Use `Z_SCORE` or `IQR` alone for broader detection.

### Severity levels

- `anomaly` — both z-score AND IQR triggered (high confidence drift).
- `warning` — only one method triggered (worth review, lower confidence).

### Limitations

- Requires ≥5 baseline samples (`MIN_SAMPLES`); insufficient data returns empty list (no anomalies, but no false negatives surfaced either).
- Cost dimension requires model in `DEFAULT_PRICES` price table (see `policy/prices.py`); spans with unknown models contribute no cost datapoint.
- Single chain.db per `RegressionDetector` instance — no cross-chain analysis in v0.3.0.

## Shutting down BIJOTEL

`shutdown()` flushes any pending spans and tears down the global TracerProvider. Important when running scripts that exit immediately (without flush, last spans may be lost).

```python
from bijotel import init, shutdown

init(...)
# ... do work, emit spans ...
shutdown()  # flushes processors, releases resources
```

`shutdown()` is idempotent — safe to call multiple times.

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

# Export chain to portable signed JSON (verifiable by external auditors)
bijotel export --db chain.db --output audit_trail.json

# Verify integrity of an exported JSON (no DB needed, just secret)
bijotel verify-export audit_trail.json
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
- [x] F7: Provider protocol + AnthropicAdapter (`@trace_genai(provider=adapter)` integration; multi-provider deferred until concrete need)
- [x] F8: Portable signed JSON chain export (`bijotel export` + `bijotel verify-export`) + `rate_limit_calls_per_minute` rule
- [x] F12: Regression Detection (Bijuteria #16) — `RegressionDetector` + `bijotel regression` CLI (z-score + IQR over input_tokens / output_tokens / cost)
- [x] F9: `OpenAIAdapter` — second concrete Provider implementation, validates F7 Protocol design empiric (zero F7 changes required). Install: `pip install bijotel[openai]`.
- [x] F11: `prompt_pattern_deny` rule — regex-based jailbreak / prompt-injection detection over `request["messages"]`. Defaults cover 5 attack categories (instruction override, system prompt extraction, role override, jailbreak framing, encoding bypass). Custom patterns + defaults composable; warn / deny mode switch. Pattern catalog adapted from substrate-guard's `agent_safety.rego` (separate project, read-only).

## License

MIT
