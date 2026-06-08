# BIJOTEL

[![PyPI](https://img.shields.io/pypi/v/bijotel.svg)](https://pypi.org/project/bijotel/)
[![CI](https://github.com/octavuntila-prog/BIJOTEL/actions/workflows/ci.yml/badge.svg)](https://github.com/octavuntila-prog/BIJOTEL/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/bijotel.svg)](https://pypi.org/project/bijotel/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-983%20passing-brightgreen.svg)](#)
[![Coverage](https://img.shields.io/badge/coverage-86%25-green.svg)](#)
[![Layers](https://img.shields.io/badge/layers-14%2F14%20active-brightgreen.svg)](#)
[![Providers](https://img.shields.io/badge/chain%20providers-Anthropic%20%C2%B7%20xAI%20%28OpenAI%20adapter%29-blue.svg)](#)

> **Tamper-evident HMAC audit chain for LLM applications.**

BIJOTEL turns the spans your OpenTelemetry GenAI instrumentation already
emits into a HMAC-sealed chain on disk, content-addressable storage with
semantic dedup, and a pre-call policy gate that audits before it blocks.
It's a plug-in to whatever tracer you have (OpenLLMetry,
`AnthropicInstrumentor`, custom wrappers) — it does not replace your
tracer; it extends it.

**Status:** v2.15.0 on PyPI; **GENA + ARA production both run v2.15.0**
(as of 2026-06-08). Validated on **2 independent
production systems**: **GENA** (9-ecosystem AI mesh, x86_64, Nuremberg
— running BIJOTEL since 2026-05-10) and **ARA** (AI Research Agency,
aarch64, Helsinki — running since 2026-05-25).
Production-validated through 28+ consecutive days on GENA: **12,900+
chain entries (GENA) + 1,400+ (ARA), both `Chain VALID`, 30+ wheel
deploys (v0.5.0 → v2.14.1), 0 chain breaks, 2 LLM providers in the
same chain** (Anthropic + xAI; the OpenAI SDK adapter is shipped).
Both chain heads are **anchored daily in Sigstore Rekor** (public,
third-party timestamps).
**All 14 bijuterii layers active** at the default `bijotel serve` engine.

## Multi-provider chain (v2.0.0)

The HMAC-sealed chain handles **any LLM provider that emits OTel GenAI
spans**, in the same table, with the same HMAC secret, the same JCS
canonical body format. Anthropic spans (via
`opentelemetry-instrumentation-anthropic`) and OpenAI / xAI spans (via
`bijotel.wrap()`) land side-by-side. `bijotel verify` walks the whole
chain without distinguishing — the HMAC linkage holds regardless of
who emitted each span.

```
chain rows on GENA (excerpt, 2026-06-03):
  seq 10606  anthropic.chat  provider=anthropic  claude-haiku-4-5
  seq 10605  anthropic.chat  provider=anthropic  claude-haiku-4-5
  seq 10604  openai.chat     provider=xai        grok-3-mini      (gen4 verifier)
  seq 10603  anthropic.chat  provider=anthropic  claude-haiku-4-5 (gen4 extractor)
  ...
  $ bijotel verify --db chain.db
  Chain VALID (10606 entries).
```

That's N-version programming in production: one provider extracts,
another verifies; the chain records both and verifies cleanly across
both.

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

## Docker

The published image bundles the `[api,fingerprint,ast]` extras and runs
`bijotel serve --dashboard` by default — REST API at `/api/*`, dashboard
at `/`:

```bash
docker run -p 8080:8080 \
  -e BIJOTEL_HMAC_SECRET=your-secret-min-16-chars \
  -v bijotel-data:/data \
  ghcr.io/octavuntila-prog/bijotel:latest
```

Then open <http://localhost:8080/> for the dashboard and
<http://localhost:8080/api/health> for the REST liveness probe. Versioned
tags (`:2.15.0`, `:latest`) live at
[ghcr.io/octavuntila-prog/bijotel](https://github.com/octavuntila-prog/BIJOTEL/pkgs/container/bijotel).

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
bijotel verify --db chain.db                          # full HMAC re-verification
bijotel inspect --db chain.db 4952                     # one entry's canonical body
bijotel stats --db chain.db                           # chain + CAS + policy stats
bijotel list --db chain.db --since 2026-05-20         # filterable browsing
bijotel export --db chain.db --output out.json        # signed portable JSON
bijotel verify-export out.json                         # auditor-side verification
bijotel regression --db chain.db --window 100         # z-score + IQR drift
bijotel serve --port 8080 --db chain.db               # REST API only (Swagger at /docs)
bijotel serve --port 8080 --db chain.db --dashboard   # API at /api/* + React dashboard at /
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

## 14 AI safety bijuterii — all active in v2.0.0

Each layer maps to a catalog pattern. ``status`` reflects the live
``GET /layers`` response on a healthy production install. There are
three states the endpoint can report:

* **active** — layer is wired in this process *right now* and has
  runtime evidence (chain rows, rule in PolicyEngine, tracker on
  app state, sibling JSON on disk, etc.).
* **available** — code ships and is importable, but nothing on this
  server currently uses it; the host can opt in via config.
* **planned** — *no code yet*. In **v2.0.0 there are zero ``planned``
  layers** — the catalog is whole.

On a fresh ``pip install`` with the v2.0.0 default engine
(``prompt_pattern_deny + pii_detection + output_length_limit +
ast_safety_check + routing_recommendation``), the layers below
report ``active`` immediately once their evidence trigger is met
(see column "active when…"). The empty-chain edge case is the only
one where ``forensic_chain``/``regression`` start as
``available`` — they flip to ``active`` after the first sealed span.

| # | Bijuterie | Layer | Active when… | v2.0.0 |
|---|---|---|---|---|
| 11 | Forensic-First | HMAC-SHA256 chain (`HmacChainSpanProcessor`) | chain table has ≥1 row | ✅ active |
| 2  | Content-Addressable Storage | CAS unique-body table | cas table has ≥1 row | ✅ active |
| 2  | Merkle DAG | `dag_nodes` + `dag_refs` reference graph | dag_nodes has ≥1 row | ✅ active (since v1.5.3) |
| 10 | Compliance-as-Code | PolicyEngine + 11 rule factories | engine on app state | ✅ active |
| 5  | AST-First Code Safety | tree-sitter bash + stdlib Python ast | `ast_safety_check` rule in engine | ✅ active (since v1.9.1) |
| 15 | Inference Routing | Pareto cost/quality/latency + per-agent budget | `routing_recommendation` rule in engine | ✅ active (since v1.6.0) |
| D  | Containment (Combo D) | Permitted + Safe + Sealed orchestrator | `ContainmentGuard` on app state | ✅ active (since v1.7.0) |
| 9  | Consensus Voting | Multi-model agreement, N-version pattern | `consensus_provider` on app state | ✅ active (since v1.8.0) |
| 3  | Energy Accounting | per-call Wh + grams CO2 + region grid intensity | `EnergyTracker` attached **or** `energy_log` rows | ✅ active (since v1.9.0) |
| 16 | Regression Detection | z-score + IQR over input_tokens/output_tokens/cost | chain has ≥5 rows | ✅ active |
| 7  | Deterministic + Semantic Fingerprinting | SHA-256 + sentence-transformers | `bijotel_fingerprints.db` has ≥1 row | ✅ active (since v1.6.0) |
| 18 | Misalignment Probes | 29 probes across 8 attack categories | `misalignment_probes_*.json` sibling exists | ✅ active (since v1.9.1) |
| 19 | OTel GenAI Semconv | Compatible with OpenLLMetry, Anthropic/OpenAI instrumentors | always (semantic conventions used throughout) | ✅ active |
| 7  | Provider Protocol | `AnthropicAdapter`, `OpenAIAdapter` (xAI via OpenAI-compatible) | always | ✅ active |

### Why no more ``planned``

Up through v1.8.0 the table carried ``planned`` for **Energy
Accounting** and ``planned`` for **Consensus Voting** — both
shipped in v1.8.0 / v1.9.0 with full tests and production proof:

* v1.8.0 — Consensus: real Haiku vs Sonnet votes recorded
  (factual agreement 1.00, creative agreement 0.15).
* v1.9.0 — Energy: 14-day GENA backfill produced
  19.95 Wh / 7.58 g CO2; Haiku-migration savings ≈ 8× captured
  ex post facto.

v2.0.0 is the tag for the moment the column emptied.

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

## Production validated (v2.14.1, 2026-06-07)

GENA's production agent ecosystem (Aisophical) has been the rolling
integration test since v0.5.0:

* **28+ days continuous operation** (2026-05-10 → 2026-06-07), **30+
  wheel deploys** on GENA spanning v0.5.0 → v2.14.1. GENA + ARA both run
  v2.14.1; the EnergySpanProcessor chain-sealing fix (v2.14.1) is live on
  both. A cross-org federation service runs on ARA with GENA + ARA as the
  first two operators (4 Rekor cross-anchors, daily cron).
* **12,900+ chain entries on GENA + 1,400+ on ARA**, `bijotel verify`
  returns `Chain VALID` on both — cross-version *and* cross-provider
  HMAC continuity. Both chain heads anchored daily in Sigstore Rekor.
* **0 chain breaks** across the 14-deploy window; the chain
  processor's ``BEGIN IMMEDIATE`` critical section + WAL mode survived
  every concurrent-writer scenario including the gen4 add-on (today)
  writing into the same DB from a separate process.
* **2 LLM providers in the chain** as of 2026-05-24:
  Anthropic (claude-haiku-4-5 + claude-sonnet-4) emitted via
  `AnthropicInstrumentor`, and xAI (grok-3-mini) emitted via
  `bijotel.wrap()` on an OpenAI-SDK client pointing at
  `https://api.x.ai/v1`. Both providers verify under the same HMAC.
* **All 14 layers `active`** in `/api/layers` on the default
  `bijotel serve` engine.
* **14-day energy footprint** (chain backfill, 2026-05-24):
  19.95 Wh / 7.58 g CO2 across 5,459 LLM calls.
  Haiku migration on 2026-05-21 cut daily CO2 ≈ 8× — captured
  retroactively, not designed in.
* **Cross-provider consensus** sample (2026-05-24): Haiku vs Sonnet
  on a factual prompt scored 1.00 agreement (same answer);
  on a creative prompt scored 0.15 (genuine disagreement → flag).

For deep production validation across Rounds 1–3 (46 tests, 0
partial/fail), see release notes in
[`CHANGELOG.md`](CHANGELOG.md) and the live demo with a
verify-yourself chain at <https://bijotel.whiteandpoint.com>.

## Known issues

* **Vite dev server binds IPv6-only on some Windows installs.**
  `curl 127.0.0.1:5173` returns nothing; use `curl localhost:5173`
  (DNS resolves to ::1) or `curl '[::1]:5173'`. Browsers are fine.
  Only affects the dashboard dev workflow; the built bundle shipped
  in the wheel is unaffected.
* **F11 pattern coverage** improved to 100% on the R1 production
  probe corpus in v2.0.5 (23/23 attack vectors caught with 0 false
  positives on the benign control set). Coverage on broader, unseen
  attack corpora is necessarily lower; pattern expansion is iterative,
  not "done". See the v2.0.5 entry in `CHANGELOG.md` for the methodology.
* **`bijotel verify` signals failure via stderr + exit code 3, not
  stdout.** A surfaced R3 finding; if you script the CLI, branch on
  exit code, not stdout substring match.

## License

MIT
