# Threat Model

BIJOTEL is a tamper-evident HMAC audit chain for LLM applications. This
page documents exactly what BIJOTEL **does** protect against, what it
**does not**, and where to reach for stronger guarantees.

The framing follows the same M2 principle the rest of the docs use:
reality beats marketing. If a threat sits outside our scope, that's
stated explicitly here rather than implied away.

---

## What BIJOTEL protects against

### Post-factum database tampering

An attacker who gains read/write access to `chain.db` **after** an entry
has been sealed cannot:

- Modify any entry's body without breaking the HMAC chain at the exact
  `seq`.
- Delete entries without creating a gap detected by `bijotel verify`.
- Insert fake entries without the HMAC secret.
- Alter `canonical_body` while keeping the chain links valid — the
  v2.0.3+ verifier re-hashes the body and compares against
  `canonical_hash`. Tampering at this layer raises
  `Export INVALID: canonical_body tampered at seq=N`.

**Verification:**

- `bijotel verify --db chain.db` walks the full SQLite chain.
- `bijotel verify-export some_export.json` verifies an exported archive
  with no SQLite or filesystem dependency on the auditor side.

### Cross-version and cross-architecture portability

Chain exports are bit-identical across platforms. A chain sealed on
x86_64 (e.g. GENA, Nuremberg) verifies identically on aarch64 (e.g. ARA,
Helsinki). JCS canonicalization (RFC 8785) makes the hash inputs
deterministic regardless of CPU, Python build, or JSON serializer.

**Evidence:** Round 2 Test D verified 5,687 entries cross-architecture
at ~15,328 entries/sec on a stock ARM64 instance.

### Silent model degradation

The F12 regression detector watches statistical drift in token counts,
costs, and latency using z-score and IQR methods over the sealed chain.

**Evidence:** an hourly `/regression/run` cron on GENA fires against
the live chain; baselines are recomputed automatically and anomalies
appear in `/api/regression/latest`.

### Prompt injection and jailbreak attempts

The F11 `PolicyEngine` evaluates prompts pre-call against 50 patterns
in 7 categories (instruction override, system prompt extraction, role
override, jailbreak framing, encoding bypass, hypothetical framing,
multi-turn priming). The `ast_safety_check` rule additionally inspects
code blocks structurally with `tree-sitter-bash` and Python's `ast`
module.

**Evidence:** 100% detection on the R1 probe corpus with 0 false
positives on the benign corpus. Probes and corpus are checked into the
repository for replay.

---

## What BIJOTEL does NOT protect against

### Insider with the HMAC secret

BIJOTEL's HMAC chain uses a **symmetric** secret. Anyone who possesses
`BIJOTEL_HMAC_SECRET` can append valid entries to the chain. This is
the same trust model as TLS pre-shared keys or git's SSH deploy keys.

BIJOTEL's HMAC tamper-evidence is for **post-factum** modification by
a party that *doesn't* hold the secret, not for malicious operators
who do.

**Mitigation:**

- Treat the secret like a database credential.
- Store it in a secrets manager (Vault, AWS Secrets Manager, GCP Secret
  Manager) — not in repo config or `.env` files in production.
- Rotate periodically. The verifier handles rotation boundaries
  correctly: `bijotel verify` returns the exact `seq` where the old
  secret stops verifying. See the
  [Secret Rotation Playbook](operations/secret-rotation.md).
- **For external auditors: use v2.1.0+ Ed25519 signed exports.** An
  auditor never needs the seal-time HMAC secret — they verify the
  export with the operator's public key only, and cannot forge entries.
  See "Auditor verification without the HMAC secret" below.

### Secret leakage

If the HMAC secret is leaked, an attacker can:

- Generate valid chain entries from scratch.
- Forge exports that pass `verify-export` against the same secret.

They still **cannot**:

- Modify existing entries in a chain they don't have write access to.
- Break the linkage of an already-distributed export without detection.

**Mitigation:** rotate the secret on suspicion and re-seal new entries
under the new key. Older entries verify under the old key by design;
the boundary `seq` is detectable.

### Database deletion or filesystem loss

If `chain.db` is deleted entirely, the audit trail is lost. BIJOTEL is
an **integrity** layer, not a **backup** system.

**Mitigation:** standard backup discipline — periodic snapshots,
periodic `bijotel export` of the chain to an external location, and
ideally a 3-2-1 backup posture for the seal host.

### Lost spans on disk pressure or write failure

If `on_end()` fails (disk full, SQLite locked beyond retry, container
killed mid-write), the span is logged at ERROR level but **not** sealed
into the chain. The host application continues unaffected (crash
isolation, v0.6.0+).

The result is a gap in the audit trail, not a host crash. The chain
itself remains `VALID` — the gap is detectable by inspecting `seq`
numbering. Round 3 Test B1 (`kill -9` mid-write) and Test B2
(read-only DB) confirmed this isolation behaviour empirically.

### Multi-writer contention at extreme scale

SQLite with WAL + `BEGIN IMMEDIATE` handles concurrent writers cleanly
up to roughly 200 spans/sec on a single chain file (Round 3 Test D2).
Past that, contention shows up as `BUSY` retries that eat into latency
budget. At extreme scale (≫1,000 concurrent writers on one chain),
consider PostgreSQL with the chain table or partition by writer
identity.

### Paraphrase and multimodal attacks

The F11 detector is regex-based. Synonym-substitution paraphrases,
unicode confusables outside the catalogued set, or multimodal attacks
(prompts embedded in images, audio, or files) are outside F11 scope.

For defence in depth, layer F11 with a model-based guard such as
Lakera Guard, Prompt Security, or Rebuff. F11 catches the cheap surface
forms quickly and cheaply; the model-based guard catches the long tail.

### Formal correctness of agent actions

BIJOTEL proves **integrity** of the audit log (the log wasn't
tampered). It does **not** prove **correctness** of what the agent
did. A perfectly sealed chain can document a perfectly disastrous
decision.

For formal verification of agent actions (Z3 SMT solver, OPA/Rego
policy decisions over actions, eBPF syscall observation, threshold
non-membership checks over training data — see the state table below
for which of these are deployed), see
[substrate-guard](https://github.com/octavuntila-prog/substrate-guard).
BIJOTEL and substrate-guard are designed to be deployed together when
the application demands both properties.

---

## BIJOTEL vs substrate-guard — scope boundary

The **State** column says what each capability does *today*, not what
its design describes. Legend:

- ● **runs in production** — exercised by a production runtime (the
  GENA/ARA chains) or by substrate-guard's nightly audit on the
  Research server.
- ◐ **implemented, not exercised in production** — module and tests
  exist; not wired into the production path, behind a flag that is
  off, or not running on a production host at measurement time.
- ○ **design / prototype** — spec, stub, or paper-era brand only.
- ❌ — not in scope for that project.

| Claim | BIJOTEL | substrate-guard | State | Evidence (file path; measured when) |
|-------|---------|-----------------|-------|-------------------------------------|
| "Log wasn't tampered" (HMAC-SHA256 chain) | ✅ | ✅ (same primitive) | ● both | BIJOTEL `src/bijotel/processors/hmac_chain.py` — GENA chain 74,771 entries, last entry 2026-09-10T13:39Z; ARA chain 21,784 entries, last 2026-09-10T10:48Z (both read 2026-09-10T13:48Z). substrate-guard `substrate_guard/chain.py` + `scripts/cron-audit.sh` — nightly audit ran 2026-09-10T04:00Z, 66 events, chain intact |
| "Agent action passed a policy check" (built-in Python rules) | ❌ | ✅ | ● | `substrate_guard/policy/engine.py`; nightly audit report `layers.policy = "builtin"` (2026-09-10T04:00Z) |
| "Agent action passed an OPA/Rego policy" | ❌ | ✅ | ◐ | `substrate_guard/policy/policies/agent_safety.rego`, `tests/test_policy_parity.py` (CI parity gate); OPA binary present on the Research server but `SUBSTRATE_GUARD_POLICY=rego` is not set in the crontab — the cron decides with the built-in engine (2026-09-10) |
| "Agent action was proven safe" (Z3 SMT) | ❌ | ✅ | ◐ | `substrate_guard/code_verifier.py`, `substrate_guard/perevent_verify.py`, `tests/test_verify/`; nightly report `layers.verify = "z3 (available, not exercised per-event in batch)"` (2026-09-10T04:00Z); no production producer attaches artifacts (`docs/releases/v13.4.3.md`) |
| "Syscalls observed at kernel level" (eBPF) | ❌ | ✅ | ◐ | `substrate_guard/observe/tracer.py`, `substrate_guard/observe/bpf_programs/agent_trace.c`; wired only in `monitor --live`, not in the cron (`layers.observe = "replay"`, 2026-09-10); the kernel branch has no test in the suite and no recorded run in the repo. Observation only — neither project enforces syscalls in the kernel |
| "Training data non-membership" (threshold check over a Merkle commitment) | ❌ | ✅ | ◐ | `substrate_guard/comply/protocol.py`, `tests/test_comply/` (6 files), CLI `comply demo`; kept out of the production import graph by `tests/test_layer_wiring.py` |
| "Training data compliant, zero-knowledge" (ZK-SNM) | ❌ | ❌ | ○ | `substrate_guard/comply/protocol.py:3-5` — "Branded ZK-SNM but NOT zero-knowledge"; the brand survives only as the certificate wire identifier |
| "Export signed with a software Ed25519 key" | ✅ (v2.1.0) | ✅ | BIJOTEL ● / substrate-guard ◐ | BIJOTEL `src/bijotel/crypto/ed25519.py` — GENA→ARA verification 2026-05-26 (section below); the ARA federation service verifies Ed25519 chain heads on every submission (`/status` 2026-09-10T13:48Z: 2 operators, 200 submissions). substrate-guard `substrate_guard/attest/device_key.py`, `tests/test_chain_head_signature.py` — "not wired into a deployed export yet" (`docs/releases/v13.4.3.md`) |
| "Signed by hardware" (TPM / TEE attestation) | ❌ (stub) | ❌ | ○ | BIJOTEL `src/bijotel/attestation/tpm2.py` raises `NotImplementedError`; substrate-guard `substrate_guard/attest/fingerprint.py:1` ("no TPM"), `attest/attested_guard.py:49` `tpm_available: False` |
| "Works offline" (local SQLite store + append-only sync) | ❌ | ✅ | ◐ | `substrate_guard/offline/local_store.py`, `substrate_guard/offline/sync.py`, `tests/test_offline/`; demo-only per `tests/test_layer_wiring.py` |
| "Works offline (CRDT merge)" | ❌ | ❌ | ○ | `substrate_guard/offline/sync.py:14-19` — "NOT a general CRDT": `INSERT OR IGNORE` union by id, no value-level merge |
| "`pip install` one-liner" | ✅ | ❌ | ● | PyPI `bijotel` 2.16.0 (`pip index`, 2026-09-10); the ARA container runs the 2.16.0 index install, GENA runs 2.15.0 (read 2026-09-10T13:49Z). substrate-guard is not on PyPI — source install only (`README.md`) |
| "Bundled REST API + dashboard" | ✅ | ❌ | BIJOTEL ● (GENA) / substrate-guard ❌ | `src/bijotel/api/app.py`, `src/bijotel/dashboard_dist/`, `tests/test_api_*.py`; runs on GENA inside container `gena-v3-atelier-1` as `bijotel serve --port 8090 --host 0.0.0.0 --db /data/bijotel_chain.db --dashboard` (2.15.0; `/api/health` ok 2026-09-10T14:17Z). The port is container-internal and not published to the host, which is why a host-side probe at 2026-09-10T13:48Z found nothing; the hourly regression cron drives it — `/var/log/bijotel/regression_api.log` last written 2026-09-10T14:30Z, results `clean`. Not deployed on ARA (:8088 there is the separate `bijotel-federation` 0.3.0 service, which imports `bijotel.crypto` and `bijotel.federation`, not `bijotel.api`) |

BIJOTEL is the PyPI-installable subset focused on LLM observability +
forensic chain — the demonstrator of bijuteria #11 (Forensic-First
Architecture) at scale. The remaining safety bijuterii live in
`substrate-guard` in the states shown above, not as production
features: #1 Z3 is installed on the Research server and exercised by
tests and the CLI, but not per-event in the nightly audit (◐); #8 eBPF
is an implemented live-monitor path that the deployed cron does not use
(◐); #6 ZK is a non-zero-knowledge threshold prototype (○ for the ZK
claim, ◐ for the threshold check); #12 hardware trust is a tested
software-key path with `tpm_available: False` — the hardware part is
design-only (○). What runs in production differs by side. On BIJOTEL,
more than the chain runs — verified from the GENA logs on
2026-09-10T14:45Z: the HMAC chain, the daily Ed25519-signed export
verification (`export_verify.log`, "Export VALID — HMAC chain + Ed25519
signature both verified", 2026-09-10T05:30:01Z), the daily federation
submission (`federation_submit.log`, 2026-09-10T03:00:01Z) and Rekor
anchoring (`anchor.log`, 2026-09-10T03:30:04Z), and — on GENA only —
the bundled REST API + dashboard with its hourly regression run
(`regression_api.log`, 2026-09-10T14:30Z). On substrate-guard, only the
HMAC chain and the built-in policy audit run in production. This page
is the canonical statement of that boundary on the docs site.

State classification verified against substrate-guard v13.4.3 (commit
`1c454be`, 2026-07-25 — the same commit installed at
`/opt/substrate-guard` on the Research server) on 2026-09-10.

---

## Secret rotation

The chain verifier handles HMAC secret rotation correctly: it detects
the exact `seq` where the old secret stops verifying and the new
secret takes over. Round 2 Test E3 confirmed boundary detection at
`seq=51` with zero false positives on either half of the chain.

The full procedure — when to rotate, how to record the boundary, how
to verify both halves of the chain, how to hand a rotated chain to an
auditor — lives in the dedicated
[Secret Rotation Playbook](operations/secret-rotation.md).

In one paragraph: generate a fresh 64-hex secret, write down the
current chain length (that's the boundary), swap
`BIJOTEL_HMAC_SECRET`, restart the sealing process, then verify each
half of the chain with its corresponding secret. The old half stays
verifiable under the old key; the new half under the new key. There
is no re-signing.

---

## Auditor verification without the HMAC secret

**Added in v2.1.0.** Pre-v2.1.0 the auditor needed the HMAC secret to
verify an export — which made the auditor a potential forger. v2.1.0
adds Ed25519 asymmetric signatures on exports as an outer attestation
layer over the unchanged HMAC chain.

```bash
# Operator side (once):
bijotel keygen --output-dir ./keys
#  → keys/bijotel_private.pem  (keep secret)
#  → keys/bijotel_public.pem   (share with auditors)

# Operator side (each export):
bijotel export --db chain.db -o export.json \
               --sign-key keys/bijotel_private.pem

# Auditor side (with only the public key — NO HMAC secret):
bijotel verify-export export.json \
                      --public-key bijotel_public.pem
# → Export VALID — Ed25519 signature verified
```

The auditor verifies the export's Ed25519 signature, then checks that
each entry's canonical body still hashes to its stored
`canonical_hash` and that the `prev_hash` chain links remain
consistent. They never see the seal-time HMAC secret, so they cannot
mint new entries that would verify under it.

**Empirical evidence (2026-05-26):** a 6,341-entry chain signed on
GENA (x86_64, Nuremberg) verified bit-identically on ARA (aarch64,
Helsinki) using only the operator's public key. No HMAC secret left
the operator host. See `tests/test_export_signed.py` for the property
tests covering signature tamper, key-swap attack, and canonical-body
tamper under auditor mode.

The v1 export format (`bijotel-chain-v1`) is still produced when
`--sign-key` is not supplied — backward-compatible with every reader
since v1.1.

---

## Reporting a finding

Security issues should go to `contact@aisophical.com` rather than the
public GitHub issue tracker. See
[`SECURITY.md`](https://github.com/octavuntila-prog/BIJOTEL/blob/main/SECURITY.md)
for the disclosure window.
