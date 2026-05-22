# Changelog

All notable changes to BIJOTEL will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — 2026-05-22 — Layers: F13 Fingerprint + F14 AST Safety

First minor release on the v0.6.x hardened foundation. Introduces
``bijotel.layers/`` — a pluggable namespace for specialty SpanProcessors
beyond the core HMAC chain + CAS + policy gate. Two layers ship in 0.7.0,
both harvested with attribution from sister Aisophical projects:

### Added — F13 / Bijuteria #7: Fingerprint layer (shipped in Day 2 commit)

- ``bijotel.layers.fingerprint.DeterministicFingerprinter`` — 384-dim
  SHA-256-based embeddings (no ML dep, CI-friendly, reproducible).
  Harvested from ``substrate-guard.comply.fingerprinter``.
- ``bijotel.layers.fingerprint.SemanticFingerprinter`` —
  ``all-MiniLM-L6-v2`` 384-dim sentence embeddings.
  Optional dep: ``pip install bijotel[fingerprint]``.
- ``bijotel.layers.fingerprint.FingerprintSpanProcessor`` —
  BIJOTEL-original SpanProcessor that on_end extracts text and persists
  fingerprints into SQLite. Same hardening pattern as hmac_chain
  (WAL + busy_timeout + DDL-in-IMMEDIATE + crash-isolated on_end).
- ``bijotel.layers.fingerprint.similarity_search`` — query the store
  for spans similar to input above a threshold. Linear scan (suitable
  to ~100K rows).
- Encoder ``protocol_id`` strings persisted with each fingerprint;
  ``similarity_search`` skips rows whose encoder differs from the query
  (embeddings from different vector spaces are not comparable).
- 28 new tests in ``tests/test_fingerprint.py``.

### Added — F14 / Bijuteria #5: AST-First Safety layer

Detects dangerous code constructs structurally rather than via string
matching. The killer-example proven in tests: string matching catches
``rm -rf`` but misses ``rm -r -f``, ``rm -fr``, ``rm -rfv``,
``rm --recursive --force``, ``rm -R -f`` — AST matching catches the
entire variant family via structural pattern (command name=rm AND args
contain BOTH a recursive flag AND a force flag).

- ``bijotel.layers.ast_safety.ASTSafetyChecker`` — pluggable scanner
  for ``"python"`` (stdlib ``ast``, always available) and ``"bash"``
  (tree-sitter, optional ``[ast]`` extra). ``check_code(code, language)``
  for direct scanning, ``check_prompt(text)`` for fenced-code-block
  extraction from LLM prompts.
- ``bijotel.layers.ast_safety.ast_safety_check`` — PolicyEngine rule
  factory. Composes naturally with F11 ``prompt_pattern_deny``:
  regex catches classic jailbreak phrasings; AST catches structural
  code-execution patterns the regex misses.
- ``bijotel.layers.ast_safety.ASTViolation`` — frozen dataclass
  recording pattern, language, node type, line, snippet (truncated 80
  chars), severity.
- Built-in pattern catalog:
  * **Python** (stdlib ast, always): ``exec``/``eval`` calls,
    ``subprocess.{run,Popen,call,...}(..., shell=True)``,
    ``pickle.{loads,load}``, ``os.{system,popen,exec*,spawn*}``,
    ``__import__(...)``.
  * **Bash** (tree-sitter, optional): ``rm`` with both r and f flags
    in any combination, ``chmod`` world-writable (octal 7XX/6XX/3XX/2XX
    or symbolic a+w/o+w), ``curl|wget URL | sh|bash|zsh`` pipe-to-shell,
    ``sudo`` (warning severity).
- Graceful optional-dep handling: bash checks silently skip if
  ``tree-sitter`` / ``tree-sitter-bash`` not installed (logged once at
  INFO level with actionable install hint). Python checks always work.
- 60 new tests in ``tests/test_ast_safety.py`` (parametrized covers
  the variant family for ``dangerous_rm``, ``chmod_world_writable``,
  ``curl_pipe_to_shell``).

### Changed

- New top-level exports (+7): ``ASTSafetyChecker``, ``ASTViolation``,
  ``DeterministicFingerprinter``, ``FingerprintSpanProcessor``,
  ``SemanticFingerprinter``, ``ast_safety_check``, ``similarity_search``.
  Public ``bijotel.__all__`` now contains 34 names (was 27).
- New optional extras: ``[fingerprint]`` (``sentence-transformers``),
  ``[ast]`` (``tree-sitter`` + ``tree-sitter-bash``). ``[all]`` updated
  to pull both.
- New core dependency: ``numpy>=1.24`` (required by Fingerprint layer's
  DeterministicFingerprinter; standard in any LLM stack).
- ``__version__`` bumped 0.6.1 → 0.7.0 (minor: new features, fully
  backward-compatible).

### Tests

- **305 passed, 6 skipped** (was 245 + 6; +60 AST tests from
  parametrized expansion of 27 unique test functions).
- Coverage maintained at ~92% (new modules at lower initial coverage;
  Python AST patterns near-fully covered, bash patterns covered for
  positive + negative cases).
- ruff clean.
- pip-audit: 0 vulnerabilities.

### Bijuterii coverage progress

- Pre-0.7.0: 7/20 implemented (F0–F12 + F11 prompt_pattern_deny)
- v0.7.0 ships: **9/20** (+#7 Fingerprint, +#5 AST-First)
- 11 remain catalogued-not-yet-implemented (target v0.8.x / v1.0.0
  per the 12-day plan)

### Provenance preserved

- Fingerprinter classes harvested from
  ``substrate-guard.comply.fingerprinter`` (Aisophical SRL, MIT, same
  author).
- tree-sitter-bash grammar from upstream
  ``tree-sitter/tree-sitter-bash`` (MIT).
- BIJOTEL-original additions: SpanProcessor wrappers, Stores,
  similarity_search, ASTSafetyChecker class structure, PolicyEngine
  integration via ``ast_safety_check``.

### Not yet deployed

GENA deploy of v0.7.0 is **deferred** — numpy + tree-sitter rebuild
warrants a planned window. The hardened v0.6.1 remains in production
on GENA. Layers are additive (FingerprintSpanProcessor + the
ast_safety_check rule are both opt-in; existing v0.6.1 deployment is
unaffected by the v0.7.0 wheel sitting unused on disk).

[0.7.0]: https://github.com/octavuntila-prog/BIJOTEL/releases/tag/v0.7.0

## [0.6.1] — 2026-05-22 — Hardening fixup (concurrent _init_db)

Patch release fixing TWO multi-process races introduced by v0.6.0's
hardening itself. Both caught empirically by the multi-writer test on
GENA Linux (the Windows-skipped path) — each revision exposed the next.

### Fixed (1/2) — WAL-set race

v0.6.0 set `PRAGMA journal_mode=WAL` unconditionally in `_init_db`.
WAL-set briefly acquires an EXCLUSIVE lock; when N processes
simultaneously init the same fresh db, the first acquires, the others
fail with `SQLITE_BUSY` *before any busy_timeout had a chance to be set*.
Symptom: `sqlite3.OperationalError: database is locked` raised from
`_init_db` in subprocesses.

- Fix: set `PRAGMA busy_timeout` FIRST so subsequent PRAGMAs survive
  contention via retry. Then check current `journal_mode` and only set
  WAL if not already WAL (idempotent fast path).

### Fixed (2/2) — CREATE-TABLE visibility race

First iteration of (1) eliminated the init crash but the multi-writer
test still lost 14 of 100 spans (chain remained VALID — no corruption —
but 14 `on_end` calls saw `OperationalError: no such table: chain`).
Root cause: with all DDL outside an explicit transaction, sibling
processes opening a fresh write connection during another process's
in-progress `_init_db` could see the file exist but not yet observe the
committed `CREATE TABLE` through WAL visibility timing.

- Fix: wrap the entire `_init_db` DDL block in `BEGIN IMMEDIATE` ...
  `COMMIT`. Concurrent `_init_db` calls now serialize at the RESERVED
  lock with busy_timeout retry, AND the resulting table is fully
  visible to all readers immediately after each commit. Multi-writer
  test now lands 100/100 spans, chain VALID.

Applied identically to `HmacChainSpanProcessor` and `CasSpanProcessor`.

### Why neither race manifested in v0.6.0 production deploy

GENA's existing chain.db already had WAL enabled and table created
(set during the pre-test master init); container starts are sequential
during `docker compose up -d`, not simultaneous. Both races require
N processes simultaneously initing a *fresh* db. The bugs were real;
production happened to dodge them.

### Tests

- 217 passed, 6 skipped (unchanged Windows suite).
- Multi-writer test on GENA Linux: 4 procs × 25 spans = **100/100
  entries**, chain VALID end-to-end, perms 0o600, journal_mode wal.

### Honest meta + documented contract

The hardening introduced both races; the hardening test caught both, in
sequence. Each fix exposed a deeper layer. The current v0.6.1 contract,
empirically pinned on GENA Linux 22 mai:

**What v0.6.1 guarantees** (empirically validated):
- **No chain corruption under concurrent writers.** `verify_chain` returns
  VALID after any number of concurrent writers on an already-initialized
  chain.db. The HMAC linkage holds; no forks possible.
- **No host crashes.** All errors caught by `on_end` crash-isolation,
  logged to `bijotel.{chain,cas}`, suppressed. The host LLM call path
  is never disturbed by chain-write failures.
- **Sequential init produces correct multi-writer setup.** When chain.db
  is initialized once (master process, or first container in a sequential
  start), then opened by N writer processes, all writers operate
  correctly: WAL enabled, busy_timeout retries on contention, BEGIN
  IMMEDIATE serializes the SELECT-prev-INSERT critical section.

**What v0.6.1 does NOT guarantee** (documented limitation):
- **Concurrent fresh-db init from N processes simultaneously is
  best-effort.** When N processes spawn at the same instant and each calls
  `HmacChainSpanProcessor(...)` on the same not-yet-existing chain.db,
  the SQLite-level concurrent `CREATE TABLE` + WAL setup races below the
  library boundary (filesystem-level locking quirks; observed
  `OperationalError: disk I/O error` and `database is locked` on fresh
  init). Some spans may be dropped during this init window. Crash
  isolation catches the errors and keeps the host running; chain
  integrity holds for spans that DO land.
- This limitation does not affect production deployment patterns
  (sequential container starts via `docker compose up -d`; one master
  init before fanning out to workers; etc.). It only matters for
  N-processes-spawn-simultaneously-on-cold-db scenarios.

Bug → fix → bug → fix → accept-and-document. The discipline test pays
off: we learned the exact shape of the limit before we shipped it as a
silent failure mode.

[0.6.1]: https://github.com/octavuntila-prog/BIJOTEL/releases/tag/v0.6.1

## [0.6.0] — 2026-05-22 — Hardening

Production-readiness foundation for ARA-class concurrent consumers. Closes
the three CRITICAL-latent gaps surfaced by the T+7d audit (DOC 03 F1, F2,
E2). No new features; all changes are correctness, isolation, and security.

The chain wire-protocol is unchanged: pre-0.6.0 chain.db files are read,
verified, and continued seamlessly. Empirically validated on GENA (4,889
existing entries → continued VALID after deploy).

### Hardened — A. Crash isolation in `on_end`

- `HmacChainSpanProcessor.on_end` and `CasSpanProcessor.on_end` now wrap
  the full body in `try/except Exception`. Any failure
  (canonicalization, hashing, sqlite write) is logged at ERROR level to
  the `bijotel.chain` / `bijotel.cas` loggers and **suppressed** — the
  host application's LLM call path is never disturbed by chain-write
  failures.
- A failed write leaves a gap of one entry; subsequent entries continue
  from the still-valid `prev_hash` of the last sealed row. Test:
  `test_chain_continues_after_failed_entry` (3 ok → 1 dropped → 3 ok,
  verify still VALID).

### Hardened — B. Multi-writer correctness (WAL + BEGIN IMMEDIATE)

- `PRAGMA journal_mode=WAL` set at db init (persists at db level).
- `PRAGMA busy_timeout=5000` on every write connection (5s retry budget
  under contention vs immediate `SQLITE_BUSY`).
- The SELECT-prev-hash → compute-hmac → INSERT critical section in
  `on_end` is now wrapped in explicit `BEGIN IMMEDIATE` (autocommit
  connection + explicit transaction). The RESERVED lock is acquired
  **before** the SELECT, eliminating the read-modify-write race across
  concurrent processes sharing the same chain.db. Without IMMEDIATE,
  two writers could read the same `prev_hash` and produce a chain fork
  caught only by `verify_chain`'s linkage check.
- Per-process `threading.Lock` retained as in-process defense-in-depth.
- Test: `test_concurrent_writers_no_chain_corruption` — 4 processes ×
  25 spans each → 100 entries, chain VALID end-to-end. (POSIX-only;
  Windows skipped due to multiprocessing spawn-fixture friction.)

### Hardened — D. Restrictive file permissions on new chain.db

- Newly-created chain.db files get mode `0o600` (owner r/w only).
  Prevents world-readable leak of prompt/response BLOBs stored in
  `canonical_body`.
- Applied **only on first creation**; existing chain.db files are
  preserved at their current permissions (M5 nothing-deleted).
- POSIX-only; silently skipped on Windows / filesystems without chmod
  semantics (logged elsewhere via the host's audit).

### Hardened — C. Reproducible builds (lockfile)

- New `requirements-lock.txt` (40 packages, pip-freeze of the verified
  venv). `pyproject.toml` keeps `>=` ranges for flexibility; lockfile
  pins exact versions for reproducible deploys.

### Hardened — E. CVE scan clean

- `pip-audit -r requirements-lock.txt --no-deps`: **0 known
  vulnerabilities**. Fixed in this release: `idna 3.13` → `3.16`
  (CVE-2026-45409, transitive via httpx/anthropic).

### Tests

- 12 new tests in `tests/test_hardening.py` (8 pass on Windows, 4
  POSIX-only skipped; full 12/12 run on GENA Linux at deploy).
- Total: **217 passed, 6 skipped** (was 209 + 2; +8 hardening +
  4 platform-skipped).
- Coverage maintained at **94%** (1404 statements / 91 missing).
- `ruff check`: all checks passed.

### Changed

- `bijotel.__version__` bumped 0.5.0 → 0.6.0.
- Version bump is **minor**: API surface unchanged, public exports
  identical, schema unchanged, wire-protocol compatible. The hardening
  is internal to processor on_end paths.

### Migration notes

- No code changes required by consumers. `bijotel.processors` exports
  unchanged.
- Existing chain.db files: read as-is, continue normally, WAL mode
  enabled on first open (one-time db-level upgrade), perms NOT changed
  (preserved). New chain.db files get 0o600.
- If your host application catches exceptions from BIJOTEL's on_end and
  reacts to them, that code is now dead: on_end never raises in 0.6.0.

[0.6.0]: https://github.com/octavuntila-prog/BIJOTEL/releases/tag/v0.6.0

## [0.5.0] — 2026-05-14

Third pattern adapted from substrate-guard (separate project at
`89.167.66.225`, read-only access). Adds a regex-based prompt-injection /
jailbreak detection rule to the policy gate. Same shape as the existing
F4 / F8 built-in rules: composable into `PolicyEngine`, supports
`deny` / `warn` modes, validates fail-safe (no patterns → `ValueError`,
not silent allow).

### Added

#### F11: `prompt_pattern_deny` rule

- **`bijotel.policy.prompt_patterns.DEFAULT_JAILBREAK_PATTERNS`**: 15
  conservative regex patterns covering 5 attack categories:
  1. Instruction override (`"ignore previous instructions"`,
     `"forget everything"`)
  2. System prompt extraction (`"reveal your system prompt"`,
     `"what are your instructions"`)
  3. Role override (`"you are now a different AI"`,
     `"pretend you are different"`)
  4. Jailbreak framing (`"DAN mode"`, `"developer mode"`,
     `"hypothetically"`)
  5. Encoding bypass (`base64:`, `rot13`, `"reverse the text"`)
- **`bijotel.policy.prompt_patterns.CompiledPatternMatcher`**: lazy-compiled
  matcher (defers `re.compile()` until first `match()` call). Case-insensitive
  by default — attacks commonly use mixed-case to evade naive string matching.
- **`bijotel.policy.prompt_patterns.get_default_patterns()`**: helper returning
  a fresh copy of `DEFAULT_JAILBREAK_PATTERNS` (callers can extend without
  mutating module state).
- **`bijotel.policy.rules.prompt_pattern_deny`**: rule factory matching the
  `PolicyEngine` `Rule` contract. Parameters:
  - `patterns: list[str] | None = None` — custom regex strings, appended after
    defaults (defaults checked first).
  - `mode: str = "deny"` — `"deny"` blocks via `PolicyDeniedError`, `"warn"`
    audits but allows.
  - `use_defaults: bool = True` — set `False` for purely custom matching.
  - **Fail-safe**: `patterns=None` + `use_defaults=False` raises `ValueError`
    rather than silently allowing everything.
- Handles three message formats: plain string content (OpenAI-style),
  multipart `[{"type": "text", "text": "..."}]` (Anthropic-style), and
  pre-serialized string `messages`. Concatenates text from all roles before
  matching.
- Truncates matched pattern in `Decision.reason` to 80 chars to avoid leaking
  giant regexes into chain.db audit records.

Pattern catalog adapted from `substrate-guard/policy/policies/agent_safety.rego`
`dangerous_patterns` concept (separate project at `89.167.66.225`, read-only
access 2026-05-10). The substrate-guard version targets filesystem / network /
shell actions; this BIJOTEL adaptation targets LLM prompts (instruction
overrides, system-prompt extraction, role overrides, jailbreak framings,
encoding bypass).

### Changed

- Top-level exports: `prompt_pattern_deny` added to `bijotel.__all__` and
  `bijotel.policy.__all__`.
- Version bumped 0.4.0 → 0.5.0 (minor: new public feature,
  backward-compatible).

### Tests

- 16 new tests in `tests/test_prompt_pattern_deny.py`: default-allow on safe
  prompt, default-deny on each of 3 categories (instruction override, system
  prompt extraction, role override), warn-mode flagging, custom-patterns
  composition with defaults, custom-only no-defaults path, no-patterns
  `ValueError`, invalid-mode `ValueError`, Anthropic multipart format,
  OpenAI string format, empty-prompt allow, case-insensitive matching,
  lazy-compilation verification, PolicyEngine integration, and
  `get_default_patterns()` mutation-safety.
- Total **209 + 2 skipped** (193 → 209, +16 from F11).
- ruff clean, coverage maintained.

### Deployment guidance

Suggested rollout: deploy in `mode="warn"` first to surface false positives
via `bijotel.policy.warning` span attributes, review for ~1 week (zero
false-positive review against production traffic), then flip to
`mode="deny"`. The defaults err on the side of detection — false positives
are easier to diagnose than false negatives in this domain (security
tradeoff favors detection).

[0.5.0]: https://github.com/octavuntila-prog/BIJOTEL/releases/tag/v0.5.0

## [0.4.0] — 2026-05-11

Second concrete `Provider` adapter (OpenAI), validating the F7 Provider
Protocol design empirically. The F7 abstraction added in v0.1.0 with a
single consumer (Anthropic) is now stress-tested with a second consumer
whose SDK shape differs substantially:

| | Anthropic SDK | OpenAI SDK |
|---|---|---|
| Call path | `client.messages.create(...)` | `client.chat.completions.create(...)` |
| Response text | `response.content[0].text` | `response.choices[0].message.content` |
| Input tokens | `response.usage.input_tokens` | `response.usage.prompt_tokens` |
| Output tokens | `response.usage.output_tokens` | `response.usage.completion_tokens` |
| Stop reason | `response.stop_reason` | `response.choices[0].finish_reason` |
| Max tokens param | `max_tokens` | `max_tokens` / `max_completion_tokens` |

**F7 design verdict: VALIDATED. Zero F7 base.py changes required.**

### Added

#### F9: OpenAIAdapter

- **`bijotel.adapters.openai_adapter.OpenAIAdapter`**: implements `Provider`
  ABC using OpenAI's `chat.completions.create` API. Lazy client init
  (importable without `openai` package; SDK resolved at first call).
  Same canonical `complete(*, messages, model, max_tokens, **kwargs)`
  signature as `AnthropicAdapter`.
- **`bijotel.adapters.openai_extractors`**: `extract_openai_request` and
  `extract_openai_response` normalize OpenAI SDK shape to BIJOTEL's
  `gen_ai.*` dict contract. Handles `max_tokens` and the newer
  `max_completion_tokens` parameter. Extracts system messages from the
  `messages[role=system]` list (OpenAI's convention).
- **`@trace_genai(provider=OpenAIAdapter())`** integration verified
  empirically: emits `gen_ai.provider.name="openai"` plus all request /
  response attributes through the existing F5 decorator. Same code path,
  different provider — proof of F7 abstraction.

#### Optional dependencies

- New extras in `pyproject.toml`:
  - `pip install bijotel[anthropic]` — Anthropic SDK
  - `pip install bijotel[openai]` — OpenAI SDK
  - `pip install bijotel[all]` — both
- `openai_adapter.py` raises `RuntimeError` with actionable install hint
  (`pip install bijotel[openai]`) if `openai` package is missing at first
  client access — adapter is importable even without the SDK.

### Tests

- 18 new tests in `tests/test_openai_adapter.py` (17 + 1 smoke skipped
  without `OPENAI_API_KEY`).
- Total **193 + 2 skipped** (176 → 193 from F9, +17 verified).
- Existing F7 tests (AnthropicAdapter, trace_genai integration) all pass
  unchanged — backward compatibility preserved.

### Changed

- Top-level exports: `OpenAIAdapter` added to `bijotel.__all__`.
- Version bumped 0.3.0 → 0.4.0 (minor: new public feature, fully
  backward-compatible).

### F7 design implications

The F7 Provider Protocol is now empirically validated with two consumers
spanning the two major SDK shapes (Anthropic-style `messages.create` and
OpenAI-style `chat.completions.create`). Adding more providers in F9.x
should follow the same pattern with zero changes to `Provider` ABC or
`ProviderResponse`:

- `GeminiAdapter` (Google) — similar to OpenAI shape
- `BedrockAdapter` (AWS) — wrapper around multiple model families
- `MistralAdapter` — OpenAI-compatible API typically

[0.4.0]: https://github.com/octavuntila-prog/BIJOTEL/releases/tag/v0.4.0

## [0.3.0] — 2026-05-10

First implementation of BIJUTERII catalog #16 (Regression Detection), built
as ``bijotel.regression`` module. Empirically motivated by patterns observed
on GENA deployment (V4 rejection log technical_depth bottleneck, bimodal
quality distribution at T+2h checkpoint) — patterns worth monitoring
temporally to catch drift early.

### Added

#### Regression Detection (F12, Bijuteria #16)

- **`RegressionDetector` class**: anomaly detection over chain.db using
  z-score + IQR methods on universal dimensions.
- **`compute_baseline()`**: rolling baseline aggregation (mean, stdev,
  percentiles, IQR) over last N spans. Returns ``DimensionStats`` or ``None``
  if insufficient samples (<5).
- **`Anomaly` dataclass**: single detection record with severity tagging
  (``warning`` if 1 method flagged, ``anomaly`` if both agree).
- **`AnomalyMethod` enum**: ``Z_SCORE`` / ``IQR`` / ``BOTH``. Default
  ``BOTH`` minimizes false positives by requiring agreement.
- **3 universal dimensions**: ``input_tokens``, ``output_tokens``, ``cost``
  (cost computed on-the-fly from ``DEFAULT_PRICES``).
- **CLI**: ``bijotel regression --db chain.db`` with optional ``--dimension``,
  ``--model``, ``--window``, ``--z-threshold``. Exit codes 0/1/2 for
  no-anomalies / anomalies-detected / invalid-args.
- **17 new tests** (5 baseline + 7 detector + 5 CLI).

### Changed

- Top-level exports: ``RegressionDetector``, ``Anomaly``, ``AnomalyMethod``,
  ``DimensionStats``, ``compute_baseline`` now in ``bijotel.__all__``.
- Version bumped 0.2.1 → 0.3.0 (minor: new public feature, backward-compatible).

### Tests

- **176 total + 1 skipped** (159 → 176, +17 from F12).
- Coverage maintained at 94% overall (regression module: 91% baseline.py,
  91% detector.py).

[0.3.0]: https://github.com/octavuntila-prog/BIJOTEL/releases/tag/v0.3.0

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
