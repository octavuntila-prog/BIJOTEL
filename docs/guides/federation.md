# Federation (cross-org chain federation)

**Status:** v2.11.0 — **client side only**. The Python client + CLI
ship today and can be exercised against any future federation service.
The federation **service itself does not yet exist** (a reference
implementation lands in `bijotel-federation`, separate repo).

## What federation buys you

The HMAC chain proves your own entries weren't tampered. Ed25519
proves a specific key signed them. Rekor (v2.9) proves they existed at
time T. **Federation proves that multiple independent organisations
agree on the state of each other's chains at a checkpoint**, just like
Certificate Transparency for TLS certs.

| Layer | What it proves | Trust root |
|---|---|---|
| HMAC chain | entries not tampered post-seal | operator's HMAC secret |
| Ed25519 | signed by a specific key | operator's Ed25519 key |
| Rekor (v2.9) | existed at time T, publicly witnessed | Sigstore Rekor log |
| Attestation (v2.10) | produced by trusted code | TPM/Nitro/SEV-SNP/SGX or software |
| **Federation (v2.11)** | **multiple orgs witnessed the same chain head** | **federation operator + peer signatures** |

For the full protocol see
[`docs/design/cross-org-federation.md`](../design/cross-org-federation.md).

## Honest scope (M2: reality > docs)

At v2.11.0:

- ✓ The Python `FederationClient` is functional against any
  conforming HTTP service.
- ✓ The `bijotel federation` CLI works with `--dry-run`, which emits
  the registration/submission payload locally with no network call.
- ✓ `bijotel federation verify` is fully local — given a receipt JSON
  it recomputes the cross-anchor hash and checks the Ed25519
  signature with no service round-trip.
- ✗ **Zero external federation operators exist.** The
  reference service skeleton is the next item shipped, in a separate
  repo. Until then, federation is a forward-looking protocol with a
  working client.

## Install

`bijotel` 2.11.0 ships the federation surface in the default install
— no extras needed:

```bash
pip install --upgrade bijotel
```

## CLI

Four subcommands under `bijotel federation`:

```text
bijotel federation register --service URL --public-key PATH --private-key PATH --org NAME
bijotel federation submit   --service URL --operator-id ID --private-key PATH --export PATH
bijotel federation verify   RECEIPT.json [--federation-key PUB.pem]
bijotel federation status   --service URL
```

### `register` — claim an operator_id

```bash
# 1. Generate an Ed25519 keypair if you don't have one yet.
bijotel keygen --out-priv ./fed-priv.pem --out-pub ./fed-pub.pem

# 2. Dry-run first — produces the JSON you would POST.
bijotel federation register \
  --public-key ./fed-pub.pem \
  --private-key ./fed-priv.pem \
  --org "Aisophical SRL" \
  --contact ops@example.com \
  --dry-run
```

Live run (against a real federation, when one exists):

```bash
bijotel federation register \
  --service https://federation.example.com \
  --public-key ./fed-pub.pem \
  --private-key ./fed-priv.pem \
  --org "Aisophical SRL" \
  --output ./registration-receipt.json
```

The wire flow is two calls — `GET /register/challenge` returns a
nonce, then `POST /register` includes an Ed25519 signature of that
nonce, proving you hold the matching private key. No passwords, no
API keys.

### `submit` — submit a signed export

```bash
# Produce a signed export of an HMAC chain range.
bijotel export --range 1-1000 --sign-key ./fed-priv.pem --out chain-1-1000.json

# Submit it to a federation. --dry-run first.
bijotel federation submit \
  --operator-id op_abc123 \
  --private-key ./fed-priv.pem \
  --export ./chain-1-1000.json \
  --dry-run

# Live:
bijotel federation submit \
  --service https://federation.example.com \
  --operator-id op_abc123 \
  --private-key ./fed-priv.pem \
  --export ./chain-1-1000.json \
  --output ./submission-receipt.json
```

The Authorization header is a self-contained bearer token —
`<operator_id>.<nonce>.<nonce_signature_b64>` — verified per-request
against your registered Ed25519 key.

### `verify` — local-only verification of a cross-anchor receipt

This is the subcommand that is **fully functional today**, even
without any federation service:

```bash
bijotel federation verify ./anchor-receipt.json
# === Federation receipt MATCH (anchor_id=anchor_2026_05_26_001) ===
#   signature_verified:               True
#   cross_anchor_hash_recomputed:     True
#   anchored_at:                      2026-05-26T18:00:00Z
#   participating_operators:          2
#   rekor_log_index:                  1511534821
```

It does two checks:

1. **Recompute `cross_anchor_hash`** from `participating_operators` +
   `anchored_at` — must match the value on the receipt.
2. **Verify the federation's Ed25519 signature** over the canonical
   payload. Optionally pass `--federation-key path/to/fed-pub.pem` to
   bind the trust anchor to an externally-known key.

Exit code 0 on match, 3 on mismatch — wire this into your CI.

### `status` — peek at a federation

```bash
bijotel federation status --service https://federation.example.com
# {
#   "operators_active": 3,
#   "operators_total": 5,
#   "last_anchor": "anchor_2026_05_26_001",
#   "version": "0.1.0"
# }
```

## Python API

The same surface in code:

```python
from bijotel import (
    FederationClient,
    verify_cross_anchor_receipt,
)

client = FederationClient("https://federation.example.com")

# Register (dry-run equivalent: build payload yourself; this is live).
receipt = client.register(
    public_key_pem=open("fed-pub.pem", "rb").read(),
    private_key_pem=open("fed-priv.pem", "rb").read(),
    org_name="Aisophical SRL",
    contact_email="ops@example.com",
)
operator_id = receipt["operator_id"]

# Submit a signed export.
sub = client.submit(
    operator_id=operator_id,
    private_key_pem=open("fed-priv.pem", "rb").read(),
    signed_export_json=open("chain-1-1000.json").read(),
)

# Verify a received cross-anchor (works offline).
import json
data = json.load(open("anchor-receipt.json"))
result = verify_cross_anchor_receipt(
    CrossAnchorReceipt(**data),
    federation_public_key_pem=open("fed-pub.pem", "rb").read(),
)
assert result["valid"]
```

## What a cross-anchor receipt looks like

```json
{
  "anchor_id": "anchor_2026_05_26_001",
  "cross_anchor_hash": "9f8c…",
  "participating_operators": [
    {"operator_id": "op_a", "chain_signature": "aa…"},
    {"operator_id": "op_b", "chain_signature": "bb…"}
  ],
  "anchored_at": "2026-05-26T18:00:00Z",
  "rekor_log_index": 1511534821,
  "rekor_url": "https://rekor.sigstore.dev/api/v1/log/entries?logIndex=1511534821",
  "federation_signature": "<b64 ed25519 sig>",
  "federation_public_key_pem": "-----BEGIN PUBLIC KEY-----\n…"
}
```

The federation signs the canonical sorted-key payload (everything
except `federation_signature` itself). The `cross_anchor_hash` is
`sha256(sorted(participants).chain_signature || anchored_at)`, so any
participant or external auditor can recompute it locally — that's the
property `bijotel federation verify` exploits.

## Threat model

What federation defends against:

- Operator silently rolling back their own chain — peers signed the
  earlier head, so the rewind is detectable.
- Federation impersonation — every receipt is signed by the
  federation's Ed25519 key; pinning the federation's public key (via
  `--federation-key`) locks the trust anchor.
- Quiet collusion between operator + federation — the cross-anchor is
  also anchored in Rekor, so even if both colluded after-the-fact,
  the Rekor entry's inclusion proof time-stamps the original state.

What federation does **not** defend against:

- A federation that lies about a non-participating operator's chain —
  it can only sign over the operators it actually saw.
- Compromised federation key — same as any signing key; rotation is
  needed (key-rotation flow is part of the design doc, §11).
- Operators colluding with each other (not with the federation) — out
  of scope; the chain federation is a witness layer, not a consensus
  layer.

## Status of the reference service

The service implementation lives in the separate repo
[`octavuntila-prog/bijotel-federation`](https://github.com/octavuntila-prog/bijotel-federation)
(skeleton next; FastAPI + SQLite + Ed25519 + Rekor anchoring).
The client in this repo is intentionally service-agnostic — it speaks
the protocol defined in
[`docs/design/cross-org-federation.md`](../design/cross-org-federation.md).
