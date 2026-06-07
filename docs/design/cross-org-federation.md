# Cross-Organization Chain Federation — Design

**Status:** IMPLEMENTED + RUNNING (service v0.2.0, 2026-06-07). This
document is the protocol spec; the reference service implementing it is
live, with GENA + ARA as the first two operators (4 Rekor cross-anchors).

**Repo:** `github.com/octavuntila-prog/bijotel-federation`

**Owner:** Aisophical SRL.

**Last revised:** 2026-05-26.

---

## 1. Problem statement

BIJOTEL today is a single-operator system. One organization runs the
chain writer, holds the HMAC secret, owns the Ed25519 signing key,
and produces signed exports its own auditors can verify. That model
is sound for the single-org case — it is what the entire `bijotel
verify` story is built on.

It breaks down the moment a regulator, an industry body, or a
multi-vendor pipeline needs to see across organizations:

- **The regulator** needs a unified view of LLM activity across N
  operators in a regulated domain (healthcare, finance, legal) but
  cannot be handed any single operator's HMAC secret without
  blowing up the trust model. Each operator's secret is *exactly*
  what stops other operators from forging entries on their behalf;
  sharing it would defeat the chain's tamper-evidence property for
  every other operator that has seen the same secret.
- **A pharmacovigilance-style cross-vendor audit** wants to confirm
  that operator A's claims about call counts at time T are
  consistent with operator B's claims about consuming A's API at the
  same time. Today the auditor receives two unrelated `bijotel-chain-v2`
  exports and has no cryptographic way to bind them in time.
- **A standards body** maintaining an attestation scheme for
  "responsible LLM operators" wants periodic public proof that
  participants are actually operating chains, not just running an
  empty `bijotel` install.

Current state: each operator can produce a signed export
(`bijotel export --sign-key ...`). An auditor receiving N exports can
verify each one independently. There is no cross-chain ordering proof,
no co-existence proof, and no operator-cohort identity layer.

**What's missing is a federation layer that lets N operators
periodically cross-anchor their chains under a public root, without
any operator surrendering their HMAC secret.** That's what this design
specifies.

---

## 2. Threat model for federation

Federation introduces a new actor (the federation service) and three
new trust questions. Each gets its own answer.

### 2.1. What the federation service can and cannot see

| Federation can see | Federation cannot see |
|---|---|
| Operator Ed25519 public keys | Operator HMAC secrets |
| Per-submission `chain_signature` (an aggregate hash) | Raw chain entries (prompts, outputs, attributes) |
| Per-submission entry count + seq range | Span content of any kind |
| Per-submission `timestamp` field | Per-row `canonical_body` bytes |
| The `bijotel-chain-v2` envelope header | The `entries` array contents |

The federation only ever receives **signed exports** — the same v2
JSON file an operator hands to their own auditors. The exports contain
hashes and signatures over per-row data, not the data itself. Compromising
the federation service therefore does not compromise any single chain:
no secret leaves operator infrastructure.

### 2.2. What attacks the federation must resist

| Attack | Defense |
|---|---|
| Federation forges an entry in operator A's chain | Impossible — federation has no HMAC secret. Operator's `verify` rejects any forged row. |
| Federation lies about which operators participated in a cross-anchor | Operators record their own per-submission receipts; each contains the cross-anchor hash they were promised. Mismatch = federation lied = move to a new federation. |
| Federation backdates a cross-anchor | Rekor `integratedTime` is signed by the Linux Foundation. Federation cannot rewrite Rekor entries. |
| Operator A submits a chain with a hidden gap | Federation rejects on continuity-check (§4.2 step 3c). The previous submission's `last_hmac_hash` must equal the new submission's `first_prev_hash`. |
| Operator A rewinds (rolls back to an earlier state) | Federation rejects: new submission's `entry_count` must be ≥ previous submission's `entry_count`. |
| Federation key compromised → adversary forges receipts | Federation key rotation procedure (§9.4) re-anchors the last N cross-anchors under the new key; old receipts remain provable via Rekor's history. |

### 2.3. What federation does and does not prove

**Federation proves:** "These N chains existed simultaneously and were
cross-anchored at time T, under operator identities X1..XN, witnessed
by the federation key and Rekor."

**Federation does NOT prove:** "These chains contain correct or
truthful content." That's the per-operator HMAC chain + Ed25519
signature + (v2.10) attestation's job. Federation is an outer wrapper,
not a replacement.

### 2.4. Operator withdrawal

Any operator can unilaterally stop submitting. The federation records
the withdrawal, marks the operator inactive, and **preserves history**
— past cross-anchors that included the operator remain valid. There is
no mechanism to retroactively unbind an operator from a past anchor;
nor should there be (it would defeat the whole point).

---

## 3. Identity model

### 3.1. Operator identity = Ed25519 public key

Each operator's identity is the Ed25519 public key they registered
with. This is the same key they already use for signed exports
(`bijotel keygen` from v2.1.0).

Two consequences:

1. **No usernames, no passwords.** Registration is pure cryptographic
   identity — the operator proves they hold the matching private key
   by signing a federation-issued nonce during registration.
2. **Operators can rotate keys** with an explicit federation message
   that links the old and new keys (§3.4). Rotation events are
   themselves Rekor-anchored.

### 3.2. Federation identity = Federation Ed25519 keypair

The federation service has its own Ed25519 keypair, generated once
at service initialization. The federation public key is:

- Published on the federation's public web page
- Anchored in Rekor at initialization (the "genesis anchor")
- Distributed with every cross-anchor receipt

Operators verify cross-anchor receipts against this key. If the
federation rotates its key, the rotation event is itself anchored to
Rekor under the *old* key, so operators can chain-of-trust from any
historical receipt back to the current key.

### 3.3. Key trust model

Three options for how the federation trusts an operator's public key
at registration time:

| Option | Mechanism | Pros | Cons |
|---|---|---|---|
| **TOFU + Rekor** | First submission anchors the registration in Rekor; subsequent submissions must match the registered key | Simple, no out-of-band step | First-use is unverified — relies on Rekor's tamper-evidence as the audit anchor |
| **Out-of-band verify** | Operator emails/calls the federation operator to confirm fingerprint | High assurance | Manual; doesn't scale; requires human trust |
| **Web of Trust** | Operator A vouches for operator B by countersigning B's registration | Decentralised | Bootstrapping problem; no clear social graph for LLM operators |

**Recommendation for v1: TOFU + Rekor anchoring.** Every registration
event lands in Rekor with the operator's public key and metadata; any
auditor can later confirm "this key was registered on date X under
org-name Y". Out-of-band verify is layered on top for high-stakes
operators (regulated industries) but isn't a precondition.

### 3.4. Key rotation

Operators rotate Ed25519 keys via a signed rotation message:

```json
{
  "type": "key_rotation",
  "operator_id": "op_abc123",
  "old_public_key": "<PEM>",
  "new_public_key": "<PEM>",
  "rotation_at": "2026-06-15T10:00:00Z",
  "old_key_signature": "<sig over (new_public_key || rotation_at)>"
}
```

The federation verifies `old_key_signature` against the *currently
registered* `old_public_key`, then atomically swaps to the new key.
The rotation message is anchored to Rekor (under both old and new
keys for chain-of-trust). All subsequent submissions must be signed by
`new_public_key`.

---

## 4. Federation protocol

### 4.1. Registration

```
   Operator                         Federation                Rekor
      │                                 │                       │
      │ 1. GET /register/challenge      │                       │
      │ ──────────────────────────────► │                       │
      │ ◄─ {nonce}                      │                       │
      │                                 │                       │
      │ 2. POST /register               │                       │
      │   {public_key, org_name,        │                       │
      │    nonce_signature}             │                       │
      │ ──────────────────────────────► │                       │
      │                                 │ 3. verify signature   │
      │                                 │ 4. assign operator_id │
      │                                 │ 5. anchor registration│
      │                                 │ ────────────────────► │
      │                                 │ ◄── {log_index, ts}   │
      │ ◄─ {operator_id,                │                       │
      │     rekor_log_index,            │                       │
      │     registered_at}              │                       │
```

The challenge-response pattern at step 1 proves the operator holds
the matching private key without exposing it. Without that, an
attacker who learns an operator's public key (which is, by design,
public) could try to register a duplicate.

### 4.2. Submission (periodic)

Daily, hourly, or on operator-defined cadence:

1. Operator runs `bijotel export --sign-key keys/private.pem --output today.json`
   to produce a `bijotel-chain-v2` signed JSON.
2. Operator POSTs to `/submit` with the JSON body and an
   Ed25519-signed Authorization header (challenge-response, same as
   registration).
3. Federation:
   - **a.** Verifies the signature on the export envelope matches the
     operator's registered public key.
   - **b.** Parses `chain_signature` (the aggregate hash that the v2
     format already carries).
   - **c.** If this is not the operator's first submission, verifies
     **continuity**: `new.first_prev_hash == previous.last_hmac_hash`.
     A break here means either the operator lost rows, rewound, or
     someone is impersonating them — all reject.
   - **d.** Verifies `entry_count` is monotonically non-decreasing
     across submissions (chain is append-only).
4. Federation stores a submission receipt and returns it to the
   operator with `submission_id`, `verified=true`, and the cross-
   anchor-pending state.

### 4.3. Cross-anchoring (the core innovation)

On a timer (recommended: every 6h) or when a threshold of pending
submissions is reached:

1. Federation enumerates all submissions since the last cross-anchor
   that haven't been anchored yet.
2. Computes the **cross-anchor hash**:

   ```
   cross_anchor = SHA-256(
     operator_A.chain_signature ||
     operator_B.chain_signature ||
     ...
     operator_N.chain_signature ||
     federation_timestamp_ns
   )
   ```

   Operators are sorted by `operator_id` lexicographically so the
   hash is deterministic regardless of submission order.
3. Federation signs `cross_anchor` with its private Ed25519 key.
4. Federation uploads `(cross_anchor, signature, federation_pubkey)`
   to Rekor as a `hashedrekord/0.0.1` entry, capturing `log_index`,
   `log_uuid`, and `integratedTime`.
5. Federation writes a `cross_anchor` record locally and returns a
   **cross-anchor receipt** to each participating operator:

   ```json
   {
     "anchor_id": "anchor_2026_05_26_001",
     "cross_anchor_hash": "abc...",
     "participating_operators": ["op_a", "op_b", "op_c"],
     "rekor_log_index": 1511534821,
     "rekor_url": "https://rekor.sigstore.dev/api/v1/log/entries?logIndex=1511534821",
     "anchored_at": "2026-05-26T18:00:00Z",
     "federation_signature": "..."
   }
   ```

The receipt is what operators (and their auditors) keep as proof of
co-existence.

### 4.4. Verification (auditor / regulator path)

The verifier holds a cross-anchor receipt and wants to confirm the
claim "operators X1..XN had chains in states Y1..YN at time T".

1. Verifier requests each named operator's signed export covering the
   relevant time window.
2. Verifier runs `bijotel verify-export` against each export
   independently — confirms each export's Ed25519 signature and (if
   the operator chooses to share the HMAC secret with the auditor for
   their own chain) the per-row HMAC.
3. Verifier extracts each export's `chain_signature` and recomputes
   the cross-anchor hash using the same formula as §4.3 step 2.
4. Verifier fetches the Rekor entry by `rekor_log_index` and confirms:
   - The hash in the Rekor entry matches the recomputed cross-anchor.
   - The public key on the Rekor entry matches the federation's
     published public key.
   - The Ed25519 signature on the Rekor entry verifies.
5. Result: **"These N chains were cross-anchored at time T, signed by
   the federation key, witnessed by Rekor."**

No HMAC secret ever changes hands. The auditor learns nothing about
the *contents* of any chain beyond what its operator chose to share.

---

## 5. API specification

Six endpoints. RESTful, JSON over HTTPS, Ed25519 challenge-response
for write operations.

### 5.1. `POST /register`

Initial registration. Challenge first (`GET /register/challenge`),
then the actual register POST with the signed nonce.

```
POST /federation/register
Content-Type: application/json

{
  "public_key_pem": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n",
  "org_name": "Aisophical SRL",
  "contact_email": "ops@aisophical.com",
  "nonce": "abc123...",
  "nonce_signature": "<base64 Ed25519 sig of nonce>"
}

→ 200 OK
{
  "operator_id": "op_a1b2c3d4",
  "registered_at": "2026-05-26T12:00:00Z",
  "rekor_log_index": 1511500001
}
```

### 5.2. `POST /submit`

Submit one signed export. Auth header carries the operator's
Ed25519-signed challenge.

```
POST /federation/submit
Authorization: Bearer <operator_id>.<nonce>.<nonce_signature_b64>
Content-Type: application/json

<contents of bijotel-chain-v2 JSON file>

→ 200 OK
{
  "submission_id": "sub_xyz789",
  "operator_id": "op_a1b2c3d4",
  "verified": true,
  "entry_count": 6442,
  "first_seq": 5400,
  "last_seq": 6442,
  "continuity_verified": true,
  "submitted_at": "2026-05-26T17:30:00Z",
  "pending_anchor": true
}
```

If continuity fails, returns 422 with a specific reason.

### 5.3. `POST /anchor` (federation-internal)

Cron-triggered or threshold-triggered. Not exposed to operators —
admin-only via the federation service's own auth.

```
POST /federation/anchor
Authorization: Bearer <federation_admin_token>

→ 200 OK
{
  "anchor_id": "anchor_2026_05_26_001",
  "cross_anchor_hash": "abc...",
  "participating_operators": ["op_a1b2c3d4", "op_b5e6f7g8"],
  "rekor_log_index": 1511534821,
  "rekor_url": "...",
  "submissions_anchored": 14
}
```

After this fires, each participating operator's GET /receipts
returns the new anchor receipt.

### 5.4. `GET /status`

Public, unauthenticated, gives operators and auditors a one-shot
health view.

```
GET /federation/status

→ 200 OK
{
  "federation_public_key": "-----BEGIN PUBLIC KEY-----\n...\n",
  "operators_active": 3,
  "operators_total": 5,
  "submissions_last_24h": 12,
  "last_anchor": {
    "anchor_id": "anchor_2026_05_26_001",
    "anchored_at": "2026-05-26T18:00:00Z",
    "participants": 3
  },
  "uptime_seconds": 8642400
}
```

### 5.5. `GET /operator/{operator_id}`

Public summary of one operator's federation history.

```
GET /federation/operator/op_a1b2c3d4

→ 200 OK
{
  "operator_id": "op_a1b2c3d4",
  "public_key_pem": "...",
  "org_name": "Aisophical SRL",
  "registered_at": "2026-01-15T00:00:00Z",
  "status": "active",
  "submissions": [
    {"submission_id": "sub_001", "submitted_at": "...", "entry_count": 100},
    ...
  ],
  "anchors": [
    {"anchor_id": "anchor_..._001", "rekor_log_index": ...},
    ...
  ]
}
```

### 5.6. `GET /anchor/{anchor_id}` + `GET /verify/{anchor_id}`

The first returns the stored anchor record; the second performs a
live verification (re-fetches Rekor, re-verifies federation signature,
re-checks operator-key signatures) and returns the verdict.

```
GET /federation/verify/anchor_2026_05_26_001

→ 200 OK
{
  "anchor_id": "anchor_2026_05_26_001",
  "valid": true,
  "checks": {
    "cross_anchor_hash_recomputed": true,
    "federation_signature_verified": true,
    "rekor_entry_present": true,
    "rekor_inclusion_proof_valid": true,
    "operator_signatures_verified": ["op_a1b2c3d4", "op_b5e6f7g8"]
  },
  "verified_at": "2026-05-26T19:00:00Z"
}
```

---

## 6. Data model

Four tables. Federation database is **stateless for chain content**:
no row holds raw entries, attributes, or HMAC inputs.

### 6.1. `operators`

```sql
CREATE TABLE operators (
    operator_id              TEXT PRIMARY KEY,
    public_key_pem           TEXT NOT NULL,
    org_name                 TEXT NOT NULL,
    contact_email            TEXT,
    registered_at            TIMESTAMP NOT NULL,
    rekor_registration_index INTEGER,
    status                   TEXT CHECK (status IN ('active', 'suspended', 'withdrawn')),
    metadata_json            TEXT  -- optional operator-provided metadata
);
CREATE UNIQUE INDEX idx_operators_pubkey ON operators(public_key_pem);
```

`public_key_pem` is UNIQUE — the same key cannot register twice.

### 6.2. `submissions`

```sql
CREATE TABLE submissions (
    submission_id           TEXT PRIMARY KEY,
    operator_id             TEXT NOT NULL REFERENCES operators(operator_id),
    chain_signature         TEXT NOT NULL,
    entry_count             INTEGER NOT NULL,
    first_seq               INTEGER NOT NULL,
    last_seq                INTEGER NOT NULL,
    first_prev_hash         TEXT NOT NULL,   -- for continuity check
    last_hmac_hash          TEXT NOT NULL,   -- for next submission's continuity check
    submitted_at            TIMESTAMP NOT NULL,
    ed25519_verified        BOOLEAN NOT NULL DEFAULT FALSE,
    previous_submission_id  TEXT REFERENCES submissions(submission_id),
    continuity_verified     BOOLEAN NOT NULL DEFAULT FALSE,
    anchored                BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX idx_submissions_operator ON submissions(operator_id, submitted_at);
CREATE INDEX idx_submissions_pending  ON submissions(anchored) WHERE anchored = FALSE;
```

`first_prev_hash` and `last_hmac_hash` are sufficient to verify
continuity across submissions without storing the chain rows themselves.

### 6.3. `cross_anchors`

```sql
CREATE TABLE cross_anchors (
    anchor_id            TEXT PRIMARY KEY,
    cross_anchor_hash    TEXT NOT NULL,
    anchored_at          TIMESTAMP NOT NULL,
    rekor_log_index      INTEGER NOT NULL,
    rekor_log_uuid       TEXT NOT NULL,
    rekor_url            TEXT NOT NULL,
    federation_signature TEXT NOT NULL,
    federation_pubkey    TEXT NOT NULL  -- snapshot of the key that signed
);
CREATE INDEX idx_anchors_anchored_at ON cross_anchors(anchored_at);
```

`federation_pubkey` is snapshotted per-anchor so post-rotation
verification still works: each anchor records exactly which key
signed it.

### 6.4. `anchor_participants`

```sql
CREATE TABLE anchor_participants (
    anchor_id     TEXT NOT NULL REFERENCES cross_anchors(anchor_id),
    submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
    operator_id   TEXT NOT NULL REFERENCES operators(operator_id),
    PRIMARY KEY (anchor_id, submission_id)
);
CREATE INDEX idx_participants_operator ON anchor_participants(operator_id);
```

Join table. Lets `GET /operator/{id}` and `GET /anchor/{id}`
efficiently list the other party.

---

## 7. Deployment architecture

Three viable patterns.

### 7.1. Option A — Centralized federation service

A single FastAPI service backed by PostgreSQL (or SQLite at low scale),
operated by a neutral party. Aisophical SRL is the natural v1 host;
in steady state, an industry body (a "Linux Foundation for LLM
operators") could take over.

**Pros**

- Simplest to build and deploy (<1000 LOC FastAPI + SQLAlchemy + Rekor
  client).
- Single source of truth for the operator registry.
- One Rekor anchoring path to maintain.
- Stateless for chain content — compromise of the service does not
  compromise any chain (§2.1).

**Cons**

- Single point of failure for *availability*.
- Trust concentration: operators must trust the federation host to
  not selectively exclude submissions.
- Mitigated by Rekor: any malfeasance leaves a public, immutable trace.

### 7.2. Option B — Decentralized (peer-to-peer)

Each operator runs a federation node. A gossip protocol (Raft-lite or
a CRDT-based set) replicates submissions; cross-anchoring requires a
quorum of nodes to agree on the participant list.

**Pros**

- No central authority. Aligns with the "no trusted middleman" ethos
  of the rest of BIJOTEL.

**Cons**

- Consensus overhead. Operators are LLM platform teams, not protocol
  engineers; running a federation node is a real operational tax.
- Bootstrapping is hard: who is the first peer?
- For 0..10 operators, the central model is genuinely simpler.

### 7.3. Option C — Hybrid

Central registry (Option A) for the operator key + identity layer;
peer-to-peer anchoring for the cross-anchor step. Operators register
once with the central service, but cross-anchoring happens between
operator-run nodes that read the registry but don't depend on it for
the anchoring path.

**Pros**

- Balances simplicity (registry is centralized) and resilience
  (anchoring path tolerates the registry being down).

**Cons**

- Two systems to maintain.
- The benefit only matters at >10 operators; below that, the central
  registry is the bottleneck anyway.

### 7.4. Recommendation

**v1: Option A (centralized).** Ship the fastest path. Aisophical
hosts as a public good. Migrate to Option C when there are ≥10
operators and a clear demand signal for peer-to-peer anchoring.

Critical property regardless of option: **the federation database
stores only signatures, hashes, public keys, and metadata** — never
chain rows, never prompts, never outputs. This makes the service safe
to operate by a third party; compromise leaks public data.

---

## 8. Privacy guarantees

What an operator hands to the federation is exactly what they'd hand
to any external auditor: a `bijotel-chain-v2` signed export. Nothing
more.

### 8.1. What the federation never sees

- HMAC secrets (these never leave the operator's infrastructure)
- Raw chain rows — neither `canonical_body` blobs nor decoded
  attribute dicts
- Prompt content (input or output)
- Model identities of specific calls (only the operator's overall
  chain_signature)
- Per-row timestamps (only the export envelope's coarse window)

### 8.2. What the federation does see

- Operator public keys
- Per-submission aggregate hash (`chain_signature`)
- Per-submission entry count + seq range
- Per-submission `first_prev_hash` and `last_hmac_hash` (32-byte hash
  values, no preimage information)
- Submission timestamps
- Operator-provided metadata at registration (org name, contact email)

### 8.3. What the federation publishes (via Rekor)

- Operator registration events (public key + org name + timestamp)
- Cross-anchor hashes
- Federation signatures over cross-anchors

A nation-state-level adversary monitoring Rekor can learn:

- Which operators participate in the federation
- How often they submit
- That co-existence happened at time T

They cannot learn what any operator was doing during that time. The
chain_signature is an opaque 64-character hex string with no
recoverable structure.

### 8.4. GDPR / regulatory framing

Per §8.1, the federation processes **no personal data** about
end-users of any operator's LLM stack. The data flowing through
federation is operator-level aggregate hashes plus operator-level
identifiers (org name, contact email — themselves arguably business
data, not personal data unless the contact is a named individual).
A privacy impact assessment would land at "low risk, data minimization
already maxed."

---

## 9. Conflict resolution

The federation must handle malformed, malicious, or accidentally
broken submissions gracefully. The discipline: prefer rejection over
silent corruption.

### 9.1. Continuity violations

`new.first_prev_hash != previous.last_hmac_hash` → reject with HTTP
422 and a specific reason:

```json
{
  "error": "continuity_violation",
  "expected_first_prev_hash": "abc...",
  "got_first_prev_hash": "def...",
  "previous_submission_id": "sub_xyz",
  "hint": "Check for a missing archive or local chain rewind."
}
```

Operator must reconcile (recover the missing rows, undo the rewind)
before the federation accepts further submissions. The previous
submission stays anchored; the new one stays in limbo.

### 9.2. Rollback / entry-count regression

`new.entry_count < previous.entry_count` → reject. Same reason class.
The chain is append-only by definition; a regression means either the
operator's chain is corrupted or they're trying to forge a smaller
history.

### 9.3. Operator withdrawal

`POST /federation/withdraw` with the operator's Ed25519 signature
over `"withdraw {operator_id} at {timestamp}"`:

- Federation marks the operator `withdrawn` (status column).
- Subsequent submissions from that key are rejected at the
  authorization layer.
- **Past cross-anchors that included the operator remain valid.**
  Withdrawal is forward-only; you cannot un-co-exist.
- Withdrawal event is anchored to Rekor for public record.

### 9.4. Federation key compromise

The nightmare scenario. Recovery sequence:

1. Federation operators detect compromise (key disclosure, signed
   anomaly, internal audit).
2. Generate a new federation Ed25519 keypair.
3. Anchor a "key rotation" record in Rekor signed by **both** the
   old key (last legitimate signature) and the new key. This gives
   verifiers a documented bridge.
4. Re-sign the last N (suggested: 90 days') cross-anchors under the
   new key, anchor each new signature in Rekor.
5. Publish the new public key on the federation's website + via the
   `GET /status` endpoint.
6. Operators update their cached `federation_public_key`.
7. Old anchors keep their original signatures (the key rotation record
   is the chain-of-trust). New anchors use the new key.

This is painful but tractable. The honest answer is: **trust the
federation operator to detect compromise.** That's the same trust
posture as Sigstore for Rekor or Let's Encrypt for ACME — there is no
zero-trust answer at the root.

### 9.5. Rekor unavailability

The cross-anchoring path depends on Rekor's public instance being
reachable. When Rekor is slow or down:

- Queue the cross-anchor record locally with status `pending_rekor`.
- Retry with exponential backoff (1m, 5m, 15m, 1h, ...).
- Once Rekor responds, populate `rekor_log_index` and notify
  participating operators.
- Operators can verify the federation signature immediately; the
  Rekor leg lights up once propagation succeeds.

Operators should treat `rekor_log_index = NULL` on a fresh receipt as
"will populate within hours," not as "anchor is invalid."

---

## 10. Implementation roadmap

| Phase | Scope | Where |
|---|---|---|
| **v1** *(this document)* | Protocol spec only. No code. | This doc. |
| **v2 (BIJOTEL v2.11.0)** | Client-side: `bijotel federation submit` + `bijotel federation verify` CLI. No service. | bijotel repo. |
| **v3** | FastAPI service skeleton (registration + submission, no cross-anchoring yet). SQLite. | New repo: `bijotel-federation`. |
| **v4** | Rekor cross-anchoring path. Anchor verifier. | Same repo. |
| **v5** | Multi-operator live test with at least one external operator (Aisophical + 1 partner). | Live deployment at `federation.bijotel.whiteandpoint.com`. |
| **v6** | Production federation service. PostgreSQL. Operator UI for non-CLI users. SLA + status page. | Hosted by Aisophical SRL (or a future industry body). |

Each phase ships behind the previous one's adoption. **v2 (client CLI)
is useful even with zero federation-service operators**: it makes the
operator-side workflow concrete and shippable. v3+ assumes there is
at least one consumer for the service.

---

## 11. Honest scope / limitations

The discipline that governs the rest of BIJOTEL also governs this
design.

### 11.1. The federation is only as trustworthy as its operator (until decentralized)

In Option A (the v1 recommendation), the federation operator is a
single trust anchor. They could:

- Selectively delay anchoring some operators' submissions to create
  bogus temporal claims.
- Refuse to register a competitor.
- Quietly suspend an operator's status without public notice.

Mitigations: every status change (registration, suspension, anchor)
is Rekor-anchored. Any malfeasance leaves a public, immutable trace.
The federation cannot quietly "unhappen" an operator's history. But
it can selectively *not act* — that's the residual risk Option A
accepts.

### 11.2. Cross-anchoring proves co-existence, not correctness

A cross-anchor proves N operators' chains existed simultaneously at
time T under specified identities. It does not say anything about:

- Whether the chains contain truthful content (per-operator HMAC +
  Ed25519 + attestation already covers that)
- Whether the operators are honestly representing their LLM activity
  (no protocol can prove this; it's a social/legal trust layer)
- Whether the operators' end-users were treated fairly (regulators
  need other instruments)

The cross-anchor is a co-existence witness. It composes with the
per-chain trust layers but does not replace them.

### 11.3. Requires ≥2 operators to be meaningful

A federation of one is just an operator with extra steps. The first
real federation event needs at least two genuinely independent
operators participating. **Currently 0 external BIJOTEL operators
exist** — this design is forward-looking for the moment that changes.

The CLI (v2.11) ships first regardless: it produces submission JSON
that operators can email to each other or to a future federation
service, even before any service is running.

### 11.4. No retroactive de-federation

If operator A and operator B were both in a cross-anchor at time T,
nothing the federation does afterwards changes that. Operator A can
withdraw — future anchors will not include them — but their
participation in past anchors is immutable. This is by design (the
whole point is non-repudiation) but worth flagging since it has
operational consequences (one bad submission stays in the public
record forever).

### 11.5. Rekor as the time anchor

The federation inherits Rekor's trust properties. If Rekor's public
instance ever shuts down or rewrites history, the federation's
historical anchors lose their independent witness. Mitigations:

- Federation snapshots Rekor's signed merkle root weekly (a defense
  against future Rekor compromise).
- v2.x of the federation could support multiple transparency-log
  backends (a "logs" list rather than a single Rekor URL) — same
  pattern Certificate Transparency uses with multiple CT logs.

### 11.6. Implementation budget

The protocol is designed to be implementable in **under 1000 LOC**
for the service: FastAPI + SQLite + Ed25519 verify + the Rekor client
already in BIJOTEL v2.9.0. The client-side CLI is even smaller
(<200 LOC; it reuses `bijotel export`). Federation is *small*; the
hard part is operating it as a public good, not building it.

---

## 12. Comparison with existing systems

| System | What it federates | Trust model | Logs | Audit pattern |
|---|---|---|---|---|
| **Certificate Transparency (CT)** | TLS certificates | Multiple centralized append-only logs operated by CAs / browsers | Multiple CT logs | "Certificate X was logged before browser Y trusted it" |
| **Sigstore Rekor** | Software signatures | Single public log run by LF | One log | "Signature X existed at time T" |
| **The Update Framework (TUF)** | Software metadata | Multi-role key hierarchy | Per-project repo | "This is the metadata the project root signed" |
| **Blockchain (Bitcoin, Ethereum)** | Transactions | Decentralised consensus | Append-only ledger | "This transaction reached consensus" |
| **BIJOTEL Federation** | LLM audit chains | Federated Ed25519 + central registry + Rekor anchoring | Federation registry + Rekor | "These chains co-existed at time T under operator identities X1..XN" |

The closest analogue is **Certificate Transparency**: multiple
operators, centralized log per CA, append-only, publicly verifiable,
no consensus overhead. Unlike CT, BIJOTEL federation has only one
log per *federation* (not per CA) — operators submit, federation
anchors, public Rekor witnesses. Closer to "Submit to one of multiple
CT logs" than to "Run your own CT log".

**Unlike blockchain:** no consensus, no token, no mining. The chain
already exists locally with per-operator HMAC; federation just adds
the cross-operator co-existence layer on top.

The mental model is best stated as: **federation is to BIJOTEL what
Certificate Transparency is to TLS.** Different content, same trust
discipline.

---

## 13. Open questions for community review

These are not blockers for shipping v1 (this document), but each one
wants feedback from prospective operators before phase v3:

1. **Cross-anchor cadence.** Recommendation is every 6h. Some
   operators may want daily (less frequent = cheaper Rekor calls);
   some may want hourly (tighter co-existence proofs). Configurable?
   Operator-selectable?
2. **Submission size limit.** The current `bijotel-chain-v2` export
   format embeds the full per-row hashes; for a chain growing at
   100k rows/day, daily submissions hit ~50 MB. Federation can either
   accept large bodies or define a "summary" export format that only
   carries the chain_signature + first/last hash. Recommendation:
   start with full v2 exports; add summary form in v3 if size
   becomes operational.
3. **What does the operator share publicly?** Today a registered
   operator's `org_name` is queryable via `GET /operator/{id}`.
   Should there be a "private" registration mode where only the
   public key is published and the org name is held privately? Useful
   for regulated operators who don't want to be discoverable.
4. **Cross-federation interop.** If multiple BIJOTEL federations
   exist (one for healthcare, one for finance, etc.), should there
   be a meta-protocol for cross-federation co-existence? Almost
   certainly yes, but deferred to v7+.
5. **Operator-to-operator direct anchoring.** Some operator pairs
   might want to cross-anchor *only with each other* without a
   central federation. This is just Option B (P2P) with N=2. The v1
   service won't block this; operators can run their own anchoring
   exchanges entirely outside the central registry.

---

## 14. Appendix — relationship to other BIJOTEL features

| BIJOTEL feature | Federation interaction |
|---|---|
| HMAC chain (v1.0) | Per-operator, untouched. Federation does not see the secret. |
| Ed25519 export (v2.1.0) | The export *is* what gets submitted. Federation verifies the signature. |
| Archival (v2.2.0) | Submissions can span across archive boundaries — federation only sees the signed export, regardless of which segment(s) it spans. |
| Multi-provider (v2.0.0) | Federation sees operators, not providers. An operator's chain may contain calls to Anthropic, OpenAI, xAI, etc. — federation only cares about the operator-level signature. |
| Rekor anchoring (v2.9.0) | Federation uses the same Rekor client + Ed25519 + sigstore-python (when v2.10 swaps to it) infrastructure. Reuse, not parallel build. |
| TEE attestation (v2.10.0) | Operators *can* include attestation sidecars with their submissions. Federation surfaces them on `GET /operator/{id}` but does not verify them centrally (TEE verification is operator-by-operator out of scope for federation v1). |
| Chain integrity monitor (v2.8.0) | Operator-local. Federation could in principle run integrity checks against the per-submission metadata; deferred to v6+. |

The federation layer is **strictly additive**: removing it from the
picture leaves every other BIJOTEL feature working unchanged.
Operators not interested in cross-organizational visibility never
need to touch the federation; their `bijotel verify` continues to be
authoritative for their own chain.
