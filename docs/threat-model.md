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
policy decisions over actions, eBPF syscall enforcement, ZK proofs
over training data), see
[substrate-guard](https://github.com/octavuntila-prog/substrate-guard).
BIJOTEL and substrate-guard are designed to be deployed together when
the application demands both properties.

---

## BIJOTEL vs substrate-guard — scope boundary

| Claim | BIJOTEL | substrate-guard |
|-------|---------|-----------------|
| "Log wasn't tampered" | ✅ HMAC chain | ✅ HMAC chain (same primitive) |
| "Agent action was safe" | ❌ | ✅ Z3 SMT + OPA/Rego |
| "No unauthorized syscalls" | ❌ | ✅ eBPF kernel layer |
| "Training data compliant" | ❌ | ✅ ZK-SNM |
| "Signed by hardware" | ❌ | ✅ Ed25519 / TPM attestation |
| "Works offline (CRDT)" | ❌ | ✅ |
| "`pip install` one-liner" | ✅ | ❌ |
| "Bundled REST API + dashboard" | ✅ | ❌ |

BIJOTEL is the PyPI-installable subset focused on LLM observability +
forensic chain — the demonstrator of bijuteria #11 (Forensic-First
Architecture) at scale. The remaining safety bijuterii (#1 Z3,
#6 ZK, #8 eBPF, #12 hardware trust) live in `substrate-guard`, not
here. The README and CHANGELOG state this explicitly; this page exists
so that scope distinction is visible from the docs site too.

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
