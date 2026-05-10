# Changelog

All notable changes to BIJOTEL will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
