# BIJOTEL

> **Forensic-grade tamper-evident audit chain for LLM applications.**

BIJOTEL turns the spans your OpenTelemetry GenAI instrumentation already
emits into a HMAC-sealed chain on disk, content-addressable storage with
semantic dedup, and a pre-call policy gate that audits before it blocks.
It's a plug-in to whatever tracer you have (OpenLLMetry,
`AnthropicInstrumentor`, custom wrappers) — it does not replace your
tracer; it extends it.

**Status:** v1.1.0 on PyPI. Production-validated through 13 consecutive
days on the GENA agent ecosystem: 4,952 chain entries, 8 wheel
upgrades, 0 chain breaks. API surface frozen for the v1.x line.

## Install

```bash
pip install bijotel
```

Optional extras:

```bash
pip install "bijotel[anthropic]"     # Anthropic SDK + instrumentation
pip install "bijotel[openai]"        # OpenAI SDK
pip install "bijotel[api]"           # FastAPI + uvicorn → `bijotel serve`
pip install "bijotel[fingerprint]"   # sentence-transformers (semantic dedup)
pip install "bijotel[ast]"           # tree-sitter (Bash AST code safety)
pip install "bijotel[all]"           # everything above
```

## Quickstart

```python
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

from bijotel.processors import HmacChainSpanProcessor, CasSpanProcessor

provider = TracerProvider()
provider.add_span_processor(
    HmacChainSpanProcessor(
        secret_key=bytes.fromhex(os.environ["BIJOTEL_HMAC_SECRET"]),
        db_path="chain.db",
    )
)
provider.add_span_processor(CasSpanProcessor(db_path="chain.db"))
trace.set_tracer_provider(provider)
AnthropicInstrumentor().instrument()

# Now every anthropic.chat call is sealed in the chain with full canonical
# body, prev_hash linkage, HMAC, and CAS-deduped body storage.
```

Generate a fresh secret:

```bash
export BIJOTEL_HMAC_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
```

Verify integrity later:

```bash
bijotel verify --db chain.db
```

## CLI

After install, the `bijotel` command exposes 8 subcommands:

```bash
bijotel verify --db chain.db                   # full HMAC re-verification
bijotel inspect --db chain.db 4952              # one entry's canonical body
bijotel stats --db chain.db                    # chain + CAS + policy stats
bijotel list --db chain.db --since 2026-05-20  # filterable browsing
bijotel export --db chain.db --output out.json # signed portable JSON
bijotel verify-export out.json                  # auditor-side verification
bijotel regression --db chain.db --window 100  # z-score + IQR drift
bijotel serve --port 8080 --db chain.db        # REST API + Swagger UI
```

`--since` uses UTC calendar dates (`YYYY-MM-DD`, lower bound inclusive
at 00:00:00Z), consistent across all subcommands.

## REST API (`bijotel serve`)

`bijotel serve` exposes 18 endpoints. Full OpenAPI 3.1 spec at
`/openapi.json`, interactive Swagger UI at `/docs`.

| Method | Path                | Description |
|--------|---------------------|-------------|
| GET    | `/health`           | Liveness + version + db_exists |
| GET    | `/version`          | Package version metadata |
| GET    | `/docs`             | Swagger UI |
| GET    | `/redoc`            | ReDoc UI |
| GET    | `/openapi.json`     | OpenAPI 3.1 spec |
| GET    | `/chain`            | Paginated chain rows, since/until filters |
| GET    | `/chain/stats`      | Aggregate counters (entries/CAS/dedup/age) |
| GET    | `/chain/{seq}`      | One entry with full canonical body |
| POST   | `/chain/verify`     | Smoke (default) or `full=true` canonical |
| GET    | `/policy/rules`     | Active rules + closure introspection |
| POST   | `/policy/evaluate`  | Dry-run a request through the engine |
| GET    | `/layers`           | 14-layer bijuterii manifest |
| GET    | `/regression/latest`| Most recent persisted run |
| GET    | `/regression/history` | Paginated timeline |
| POST   | `/regression/run`   | Execute fresh run (optionally persist) |
| POST   | `/export`           | Download a signed JSON snapshot |
| POST   | `/export/verify`    | Upload + return validity + reason |

### Optional Bearer auth

Set `BIJOTEL_API_KEY` on the serve process and all endpoints (except
`/health`, `/version`, `/docs`, `/redoc`, `/openapi.json`) require
`Authorization: Bearer <key>`. Constant-time comparison
(`hmac.compare_digest`). Empty / unset env = no auth (dev mode).

## Dashboard

A React/Vite dashboard ships in `src/bijotel/dashboard/`:

| Page                | URL          | What it does |
|---------------------|--------------|--------------|
| Chain Explorer      | `/chain`     | 4 stats cards + paginated table + click-row → side panel with full canonical body; Verify + Export buttons |
| Policy Decisions    | `/policy`    | Active-rules grid + **live Evaluate form** (dry-run a prompt) + layers grid |
| Regression Monitor  | `/regression`| Status cards + recharts timeline (24h/7d/30d/all) + per-dimension breakdown + Run-Now panel |
| System Status       | `/system`    | Full 14-layer manifest table |

Bundle stays under 100 KB gzip on initial load thanks to per-route code
splitting; the heavy `recharts` chunk lazy-loads only when
`/regression` is visited.

Dev:

```bash
cd src/bijotel/dashboard
npm install
npm run dev   # http://localhost:5173 with /api proxied to :8080
```

Production build → `dashboard_dist/` at project root. Day 12 polish
wires `bijotel serve --dashboard` to mount it as static.

## 13 AI safety bijuterii covered

Each layer maps to a catalog pattern. ``active`` = wired in this
process (with runtime evidence); ``available`` = code ships, host can
opt in; ``planned`` = v1.3+.

| # | Bijuterie | Layer | Status |
|---|-----------|---------------------|--------|
| 11 | Forensic-First | HMAC-SHA256 chain (`HmacChainSpanProcessor`) | active |
| 2  | Content-Addressable | CAS + Merkle DAG | active |
| 10 | Compliance-as-Code | PolicyEngine + 8 rule factories | active |
| 16 | Regression Detection | z-score + IQR over input_tokens/output_tokens/cost | active |
| 19 | OTel GenAI Semconv | Compatible with OpenLLMetry, Anthropic/OpenAI instrumentors | active |
| 7  | Provider Protocol | `AnthropicAdapter`, `OpenAIAdapter` | active |
| 7  | Deterministic + Semantic Fingerprinting | SHA-256 + sentence-transformers | available |
| 5  | AST-First Code Safety | tree-sitter bash + stdlib Python ast | available |
| 15 | Inference Routing | Pareto cost/quality/latency + per-agent budget | available |
| 18 | Misalignment Probes | 29 probes across 8 attack categories | available |
| D  | Containment (Combo D) | Permitted + Safe + Sealed orchestrator | available |
| 3  | Energy Accounting | per-call kWh + carbon estimate | planned |
| 9  | Consensus Voting | Multi-model agreement | planned |

## What makes BIJOTEL different

* **HMAC-SHA256 tamper-evident chain.** Each span carries
  `prev_hash || canonical_hash` re-hashed with a server secret. Any
  mutation — even reordering — breaks verification. The
  ``bijotel-chain-v1`` export schema lets external auditors verify with
  the secret alone, no SQLite access.
* **Content-addressable storage with semantic dedup.** Identical
  request bodies share storage; the dedup factor surfaces as a metric
  (`/chain/stats` field). The Merkle DAG layer (`#2`) enables
  reference-graph queries.
* **Pre-call policy gate with audit trail.** Eight rule factories
  (`prompt_pattern_deny`, `pii_detection`, `output_length_limit`,
  `model_allowlist`, `model_version_pin`, `cost_per_call_max`,
  `daily_token_budget`, `rate_limit_calls_per_minute`) compose into a
  `PolicyEngine`. Decisions: ``allow`` / ``warn`` / ``deny``. Warnings
  attach to the span via `bijotel.policy.warning`. Denies emit a
  synthetic chain entry with `bijotel.blocked=true`.
* **Statistical regression detection on the chain itself.** No
  separate metrics pipeline. `RegressionDetector` reads from
  `chain.db`, computes baseline + flags drift on input_tokens /
  output_tokens / cost using z-score AND IQR (default `BOTH` mode
  minimizes false positives).
* **Composable with upstream OTel instrumentors.** BIJOTEL adds
  ``SpanProcessor``s on top of your existing
  ``opentelemetry-instrumentation-anthropic`` /
  ``opentelemetry-instrumentation-openai`` chain. It never wraps the
  SDK call itself, so there's no provider-specific glue to maintain.

## Production validated

The v1.1.0 release passed a Day-10 integration test against GENA's
production agent ecosystem (Aisophical):

* **13 days continuous operation**, 4 wheel upgrades
  (v0.5.0 → v0.6.0 → v0.6.1 → v1.1.0).
* **4,952 chain entries**, `POST /chain/verify` with `full=true`
  returns `valid:true` — cross-version HMAC continuity.
* **0 chain breaks** across the upgrade window; the chain processor's
  ``BEGIN IMMEDIATE`` critical section + WAL mode survived all
  concurrent-writer scenarios that came up.
* First **production regression baseline** persisted: cost
  $0.0033 ± $0.0008 per call (24% relative stdev); ~396 spans/day.
* **18/18 endpoints** responded correctly against the live chain.
* `POST /export` produced a 48.1 MB signed snapshot;
  `POST /export/verify` confirmed validity.

Full report in
[`INTEGRATION_TEST_20260523.md`](INTEGRATION_TEST_20260523.md).

## Known issues

* **Dashboard `bijotel serve --dashboard` not wired yet** (planned
  v1.3+). Today's pattern: run `bijotel serve` on the backend, run
  Vite dev server (or any static host) on the dashboard side, point
  the proxy at `:8080`.
* **Vite dev server binds IPv6-only on some Windows installs.**
  `curl 127.0.0.1:5173` returns nothing; use `curl localhost:5173`
  (DNS resolves to ::1) or `curl '[::1]:5173'`. Browsers are fine.
* **GENA-style deploys** that install the wheel without extras must
  add `python-multipart` to their `requirements.txt` if they want
  `POST /export/verify` to register (FastAPI's `UploadFile` requires
  it). The `[api]` extra carries it transitively.
* **GitHub source repository remains private** during the v1.x.x
  development window — the URLs in PyPI metadata (Documentation,
  Issues, Changelog, Source) currently 404 for external visitors.
  This is intentional and will flip when the repo goes public.

## License

MIT
