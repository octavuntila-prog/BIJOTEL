# Changelog

All notable changes to BIJOTEL will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] — 2026-05-10

Patch release. No API changes. Bugfix + documentation + coverage push.

### Fixed

- **Cost field calculation in `bijotel inspect` / `bijotel list`**
  Pre-v0.2.1, `_calc_cost` had two bugs discovered empirically post-deploy:

  1. `claude-sonnet-4-20250514` (production model on GENA) was missing from
     `DEFAULT_PRICES` — every Sonnet 4 call returned `?`. Fixed: added
     `claude-sonnet-4-20250514` and `claude-sonnet-4` aliases to the price
     table in `policy/prices.py`.

  2. Tiny Haiku calls (~14 input + 4 output tokens, $0.0000272) rounded to
     `$0.0000` at 4-decimal precision, indistinguishable from blocked spans
     (which truly have zero cost). Fixed: `<$0.0001` is now returned for
     real-but-tiny costs; `$0.0000` reserved for actually-zero (zero tokens).
     `?` enriched with model name fragment for actionable feedback when a
     model is missing from the price table.

### Documented

- README sections added for 6 previously-undocumented public API exports:
  `PolicyDeniedError`, `PolicyEngine`, `model_allowlist`, `shutdown`,
  `export_chain` (Python API), `verify_export` (Python API).
- "Policy Gate" section with `PolicyEngine` direct-usage example.
- "Chain export — programmatic API" section with code example.
- "Shutting down BIJOTEL" section with rationale.

### Improved

- `cli/commands.py` coverage: **75.1% → 90%** (+58 missing lines tested).
  Added `tests/test_cli_export.py` (8 tests) and `tests/test_cli_helpers.py`
  (7 tests) covering CLI subcommand paths, error handling, edge cases.
- Overall package coverage: **91.1% → 95%** (964 → 969 statements).
- Test suite: **135 → 159** tests (+24, all green).

[0.2.1]: https://github.com/octavuntila-prog/BIJOTEL/releases/tag/v0.2.1

## [0.2.0] — 2026-05-10

Patterns adapted from substrate-guard (separate project at `89.167.66.225`,
read-only access). Two features ported with attribution: portable chain
export and rate-limit policy rule.

### Added

#### Portable signed JSON export (F8)

- **`export_chain(db, output_path, secret_key)`**: dump SQLite chain to
  portable JSON file with file-level `chain_signature` (HMAC of head_hash +
  entries_count). External auditors verify with shared secret only — no DB
  access needed.
- **`verify_export(path, secret_key)`**: full integrity check with
  fail-fast diagnostics:
  - JSON parseable
  - Format identifier (`bijotel-chain-v1`)
  - `chain_signature` matches recomputed
  - Per-entry `hmac_hash` matches recomputed
  - `prev_hash` chain links unbroken
- **CLI**: `bijotel export --db chain.db --output audit.json` and
  `bijotel verify-export audit.json` (both honor `BIJOTEL_HMAC_SECRET` env).
- Schema: `bijotel-chain-v1` with base64-encoded `canonical_body` for
  binary-safe transport.

Pattern adapted from `substrate-guard/chain.py::export()` /
`verify_export()` (separate project).

#### Rate-limit policy rule (F8)

- **`rate_limit_calls_per_minute(max_calls, db_path, mode)`**: sliding
  60-second window rate limiter using SQLite-backed state.
- Atomic prune-and-check pattern (DELETE old timestamps + COUNT + INSERT).
- `mode="deny"` (default) blocks; `mode="warn"` audits but proceeds.
- Persists across rule instances (state in SQLite, not in-memory).

Pattern adapted from `substrate-guard/policy/policies/agent_safety.rego`
("api_calls_last_minute > 100" deny rule), translated to Python rule
matching BIJOTEL F4 pattern.

### Changed

- BIJOTEL `__version__` bumped from `0.0.1` to `0.2.0` (minor: new public
  features, backward-compatible).
- Top-level exports: `export_chain`, `verify_export`,
  `rate_limit_calls_per_minute` now in `bijotel.__all__`.

### Tests

- 21 new tests (12 export + 9 rate_limit), 95 + 19 (F7) existing pass
  unchanged → **135 total + 1 skipped smoke**.

[0.2.0]: https://github.com/octavuntila-prog/BIJOTEL/releases/tag/v0.2.0

## [0.1.0] — 2026-05-10

First public alpha. Tamper-evident audit chain + content-addressable storage
+ in-process policy gate, built as plug-in library on top of OpenTelemetry.

### Added

#### Core (F0–F3)

- **F0**: Project skeleton, schema discovery via 3 real Anthropic calls
- **F1**: TracerProvider initialization, GenAI semantic conventions support
- **F2**: HMAC-SHA256 audit chain with JCS canonicalization (RFC 8785)
  - SQLite-backed append-only chain
  - Tamper detection via cryptographic hash chain
  - `bijotel verify` CLI command
- **F3**: Content-addressable storage (CAS)
  - Input-only semantic dedup (excludes output/usage/timestamps from body hash)
  - Reference counting via `INSERT ON CONFLICT DO UPDATE`
  - `semantic_body_hash` column linked to chain entries

#### Policy Gate (F4)

- **F4**: In-process policy gate with 3-state decisions (`allow` / `warn` / `deny`)
  - `cost_per_call_max` rule with USD threshold
  - `daily_token_budget` rule with rolling window
  - `model_allowlist` rule for provider/model restrictions
  - Anthropic price table (2026-05) with 180-day staleness warning
  - PII redaction: `redact_input=True` replaces input with sha256 hash
  - Synthetic span emission on deny (audit trail without SDK call)
  - `guard()` decorator + `PolicyDeniedError` exception

#### Decorator + Wrap (F5)

- **F5**: `@trace_genai` decorator + `wrap()` runtime
  - Sync + async auto-detection via `asyncio.iscoroutinefunction`
  - Hybrid extractors: defaults + custom callable override
  - Defensive OTel attribute coercion (handles list/dict from custom extractors)
  - Anthropic-style request/response extractors as defaults

#### CLI (F6)

- **F6**: `bijotel` CLI with subcommands
  - `verify` — chain integrity check
  - `inspect <seq>` — single span detail with cost calculation
  - `stats` — chain statistics + dedup factor
  - `list` — query spans with filters (`--blocked`, `--rule`, `--since`, `--model`)
  - `BIJOTEL_HMAC_SECRET` env var for secret (no shell history risk)

#### Provider Adapters (F7)

- **F7**: `Provider` Protocol + `AnthropicAdapter` + `trace_genai(provider=)` integration
  - `Provider` ABC with 4 abstract methods (`name`, `extract_request_attrs`,
    `extract_response_attrs`, `complete`)
  - `ProviderResponse` frozen dataclass mapping to `gen_ai.*` attributes
  - `AnthropicAdapter` implementation reusing F5 extractors (no duplication)
  - `trace_genai(provider=AnthropicAdapter())` auto-extracts everything
  - 100% backward-compatible with F5 string `provider="anthropic"` usage
  - Explicit `request_extractor=` / `response_extractor=` always override
    adapter-supplied methods (escape hatch preserved)

#### Validation

- E2E smoke test (`scripts/e2e_smoke.py`) — full stack on real Anthropic
- 114 unit tests + 1 skipped (smoke without API key)
- ruff + mypy clean
- CI green via GitHub Actions on every push

#### Production deployment

- Deployed on 4 GENA ecosystems (V3-atelier, V4-piața, V9-oracle, V8-ambasador)
  on 2026-05-10
- Dual observer coexistence with `substrate_v2_trace.py` verified empirically
- Sub-task 0 confirmed wrapt-based instrumentation + instance-level monkey-patch
  coexist regardless of activation order
- Memory overhead: ~3–10 MB per container (vs control group)
- Chain integrity: VALID across all initial spans
- Baseline snapshot tooling (`scripts/gena_deploy/`) for T+24h+ checkpoints

### Known Limitations

- Streaming responses: deferred to F7.1+
- Tool use specific handling: deferred to F7.1+
- Vision (multimodal): deferred to F7.1+
- Multi-provider concrete adapters (OpenAI / Gemini / Bedrock / Mistral):
  deferred to F7.2+ (Provider contract ready)
- `registry.py` for adapter lookup: deferred to F7.2 (YAGNI for single adapter)
- Cost calculation in `bijotel list` may show `$0.0000` for some spans —
  on-demand calc from price table; consistency improvements deferred to F8+
  (traces.db remains authoritative for billing)

### Dependencies

Required:
- `opentelemetry-api>=1.27.0`
- `opentelemetry-sdk>=1.27.0`
- `opentelemetry-semantic-conventions>=0.48b0`
- `rfc8785>=0.1.4` (JCS canonicalization)

Optional (`[anthropic]` extra):
- `anthropic>=0.40.0` (for `AnthropicAdapter` usage)
- `opentelemetry-instrumentation-anthropic>=0.40.0` (for upstream instrumentation pattern)

### Compatibility

- Python 3.11+
- Tested with `anthropic` SDK 0.40.0 and 0.100.x
- OTel 1.27.0+

[0.1.0]: https://github.com/octavuntila-prog/BIJOTEL/releases/tag/v0.1.0
