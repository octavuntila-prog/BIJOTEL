# BIJOTEL implementation notes on draft-sharif-agent-audit-trail-00

**Document:** [`draft-sharif-agent-audit-trail-00`](https://datatracker.ietf.org/doc/draft-sharif-agent-audit-trail/)
(Individual submission, March 29 2026, expires Sep 29 2026)
**Author of draft:** R. Sharif, CyberSecAI Ltd.
**Author of this note:** Octavian Untilă (BIJOTEL maintainer)
**Date:** 2026-05-27
**Status:** Internal reference; may be shared with the draft author if helpful

> The draft has **no formal IETF working group standing** and is **not
> endorsed by the IETF**. This note is implementation-experience input
> only — not a review, not a critique, not a vote.

---

## 1. Context

BIJOTEL (`pip install bijotel`, MIT) is a tamper-evident HMAC audit
chain for LLM applications, in production on two independent systems
(GENA + ARA) since May 2026. As of 2026-05-27:

| Metric | Value |
|---|---|
| Production chain entries (GENA) | 6,500+ |
| Continuous operation | 16 days |
| Independent systems | 2 (x86_64 + aarch64) |
| Automated tests | 919 (passing) |
| Architecture portability validated | yes (cross-machine verify) |
| External anchoring | Sigstore Rekor (v2.9, design + library) |
| Cross-org federation | client + design doc (v2.11) |
| MCP invocation sealing | v2.12.0 (shipped 2026-05-27) |

This document captures what we've learned from running a production
hash-chained audit trail that is *broadly aligned with the goals of
AAT*, with the goal of being useful to the draft author and to anyone
else navigating the same design space.

## 2. Points of alignment

Where BIJOTEL and AAT make compatible choices:

| Choice | AAT | BIJOTEL | Notes |
|---|---|---|---|
| Hash algorithm | SHA-256 | SHA-256 (inside HMAC) | aligned |
| Canonicalization | RFC 8785 JCS (mandated) | RFC 8785 JCS | byte-identical in our cross-machine tests |
| Append-only | yes | yes | aligned |
| Per-record timestamp | RFC 3339 UTC | nanosecond integer (epoch ns) | semantically same |
| Action classification | controlled vocabulary | OTel GenAI semconv `gen_ai.operation.name` | different namespace, same intent |
| Trust levels | L0–L4 | layer attribution (`bijotel.layer`) | different model, similar intent |
| Optional ECDSA signature | P-256, IEEE P1363 r∥s, base64url | Ed25519 (v2.1+) | different curve; both sound |
| JSONL export | "primary" | JSONL v2 (signed) | aligned |

The most striking alignment is **JCS as a hard MUST**: this matched our
own production experience. Anything weaker (e.g. `json.dumps(...,
sort_keys=True)` alone) produces non-canonical output on
non-ASCII payloads and breaks cross-machine verification.

## 3. Implementation experience that may inform the draft

### 3.1 HMAC vs plain SHA-256 chain

**Draft:** `prev_hash = hex(SHA-256(JCS(previous_record)))`.
**BIJOTEL:** `hmac_hash = HMAC-SHA256(secret, prev_hash ∥ canonical_hash)`.

The difference: an HMAC chain requires a secret-keyed integrity check.
An attacker who can write to the storage but does **not** possess the
HMAC secret cannot forge a record that the chain will accept on verify.
A plain SHA-256 chain can be forged trivially by an attacker who can
write to storage (the entire chain re-computes deterministically from
new content).

**Trade-off:** HMAC adds operational cost — secret rotation, secret
distribution, secret storage hygiene. We have a [secret-rotation
playbook](../operations/secret-rotation.md) we maintain alongside the
spec, and we verified rotation boundary detection in production at
exact `seq=51` (test R2-E3).

**Suggestion (if the author finds it useful):**
Consider treating HMAC as a SHOULD where insider-threat is in scope,
with the plain-SHA chain remaining MUST as a baseline. Operators who
care primarily about external-tamper detection can stay on the SHA
chain; operators who care about insider-threat (e.g. cloud
deployments with privileged operators) can opt into HMAC without
diverging from the spec.

### 3.2 Secret/key rotation procedure

**Draft Section 5.7** lists `"key_rotation"` as a vocabulary value
under the lifecycle event type, but does not specify how a verifier
should handle a chain that spans a rotation.

**BIJOTEL production experience:** at rotation, we emit a *boundary
marker* record whose `canonical_body` includes both the new key
fingerprint and the previous-segment terminal hash. Verifiers walk the
chain segment-by-segment, switching keys at the boundary. We have a
test (R2-E3) that confirms rotation boundary detection at exact
`seq=51`.

**Suggestion:**
Add a normative section on rotation procedure — at minimum, the
record format that verifiers can use to detect a rotation boundary,
and the expected verifier behaviour across segments. The current
vocabulary mention is insufficient for an implementer.

### 3.3 Ed25519 vs ECDSA P-256

**Draft:** ECDSA P-256, IEEE P1363 r∥s, base64url.
**BIJOTEL:** Ed25519 (since v2.1), PKCS#8 PEM keys, base64.

Both are sound. Ed25519 is simpler (deterministic signatures, no
secure-random requirement at sign time), faster (~2x in our
benchmarks), and produces shorter signatures (64 bytes vs 64 bytes for
P1363 P-256 — same length, but Ed25519 doesn't require ASN.1
unwrapping). ECDSA P-256 has wider HSM/FIPS support.

**Suggestion:**
Consider algorithm agility — either an algorithm OID in the signature
envelope, or a SHOULD/MUST split where Ed25519 is SHOULD-implement and
ECDSA P-256 is SHOULD-implement, with operators choosing. The current
single-algorithm mandate is a forward-compatibility risk.

### 3.4 Chain segmentation and archival

**Draft Section 6** defines per-session genesis + close records with a
`session_hash`. Multi-session archival is described as "store
session_hash values in a separate, append-only system" but the
mechanism is not specified.

**BIJOTEL production experience:** at 6,500 entries / 16 days, our
chain.db is ~88 MB. At our projected 100k entries the file is ~1.3 GB
and `verify` takes ~4.4 seconds (R2-B3 benchmark). We ship CLI commands
for *range verify* (`bijotel verify --range 1-10000`) and *archival*
(`bijotel archive --range 1-50000 --to archive.db`) since v2.2.0. The
archival produces a self-contained, individually-verifiable segment
linked to the live chain by `previous_segment_hash`.

**Suggestion:**
Specify a normative archive record format. A minimum viable spec would
define:
1. The hash linking an archived segment to its successor (the
   "boundary hash"),
2. How a verifier walks across segment boundaries,
3. Whether segment exports carry their own session-hash chain
   (yes/no/optional).

This becomes increasingly relevant beyond ~1M records, where naive
"verify the whole chain" becomes operationally expensive.

### 3.5 Cross-architecture portability

**BIJOTEL test result:** chains produced on x86_64 verify byte-identically
on aarch64, including HMAC and Ed25519 signatures, at 15,328 entries/sec
(R2-D test).

The draft is silent on cross-architecture determinism. Implementers
might assume JCS + SHA-256 trivially gives portability, but in
practice:

- Some JSON libraries serialize floats non-deterministically across
  CPU architectures.
- `json.dumps(..., ensure_ascii=False)` is critical for non-Latin
  scripts — `ensure_ascii=True` produces different byte output than
  raw UTF-8.
- Integer overflow handling differs across language runtimes (e.g.
  `>2^53` numbers in JavaScript JCS implementations).

**Suggestion:**
Add a paragraph noting that JCS + SHA-256 gives architectural
portability *if and only if* the JCS implementation correctly handles
the I-JSON profile (RFC 7493). Reference the I-JSON profile explicitly.

### 3.6 Environmental impact logging (forward-looking)

EU AI Act Article 12 includes log-retention requirements for
high-risk AI systems. Article 12(2) is general ("logging shall enable
the monitoring of operation"). Whether environmental impact must
appear in the log depends on national implementation acts (still being
written as of May 2026).

**BIJOTEL production experience:** since v1.9, our chain entries
include energy estimates (Wh per call) and CO2 estimates (gCO2e per
call), based on EnergyEstimator and CarbonCalculator components. These
land as `bijotel.energy.*` and `bijotel.carbon.*` span attributes — no
schema disruption to the OTel core fields.

**Suggestion:**
Consider adding `environmental_impact` as an OPTIONAL field at the
record level, with sub-fields `energy_wh: number` and
`co2_grams: number`. Marking it OPTIONAL keeps the spec future-proof
without imposing a measurement burden on every implementer. A
forward-compatible namespace (e.g. `extensions.environmental_impact`)
would be even cleaner.

### 3.7 MCP-specific extension (forward-looking)

The AAT draft uses an `action_type` controlled vocabulary. MCP tool
invocations don't have an obvious bucket in the current vocabulary —
they're not exactly `tool_call` (which is LLM tool-calling) and not
`api_call` (which is HTTP-style).

**BIJOTEL approach (v2.12.0):** we added an `mcp.tool.*` span family
with its own attribute namespace (`bijotel.mcp.*`). This avoids
overloading existing AAT vocabulary, but it does mean external
auditors need to know two namespaces.

**Suggestion:**
Either (a) add `mcp_invocation` to the AAT vocabulary, or (b) add a
section "5.X Extension namespaces" defining how implementations may
introduce new action types without colliding with the core vocabulary.
The MCP ecosystem is large (40+ CVEs in Jan–Apr 2026, NSA CSI in May
2026) and isn't going away — AAT will benefit from a clear story here.

## 4. Data points we can contribute

If the draft author or a future working group wants empirical data,
we have:

- **Performance benchmarks** at the scales mentioned above (R3-D
  test family).
- **Concurrent-writer correctness** under SQLite WAL + BEGIN IMMEDIATE
  (R2-B1 test).
- **Tamper-detection roundtrip** including the 6 known-bad mutation
  classes (Test 1 from R3).
- **Multi-provider chains** (Anthropic + OpenAI in the same chain.db,
  d9 cross-provider sanity test family).
- **Cross-org federation receipts** (v2.11 — Ed25519-signed,
  externally verifiable).
- **MCP invocation sealing** (v2.12.0 — hashes of tool input/output,
  status, transport, duration).

All testing artifacts are reproducible from the public repo
([github.com/octavuntila-prog/BIJOTEL](https://github.com/octavuntila-prog/BIJOTEL))
under MIT. We're happy to share specific numbers or run additional
benchmarks if useful.

## 5. Format of this note

This note is not a formal IETF review. It's implementation feedback
from a single production deployment. The draft author may use, ignore,
or excerpt anything here without attribution requirements. If any of
the suggestions land in a future draft revision, that's a win for both
specs; if none do, that's also fine — different design choices serve
different operator profiles.

## 6. References

- Draft: `draft-sharif-agent-audit-trail-00`
- BIJOTEL repository: `github.com/octavuntila-prog/BIJOTEL`
- BIJOTEL threat model: `docs/threat-model.md`
- BIJOTEL secret rotation: `docs/operations/secret-rotation.md`
- BIJOTEL chain archival: `docs/operations/chain-archival.md`
- bijotel-mcp design: `docs/design/bijotel-mcp.md`
- RFC 8785 — JSON Canonicalization Scheme
- RFC 7493 — I-JSON Profile
- FIPS 186-5 — Digital Signature Standard (ECDSA, EdDSA)
- EU AI Act, Article 12 — Logging requirements
