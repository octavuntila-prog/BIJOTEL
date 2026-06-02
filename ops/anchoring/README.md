# Rekor anchoring — operator public keys

BIJOTEL anchors each production chain's head to the public **Sigstore Rekor**
transparency log daily (`bijotel anchor publish`, ECDSA P-256 — see
v2.13.2 / `bijotel.crypto.ecdsa_p256`). These are the operator **public**
keys: anyone can use them to independently verify that a given anchor
genuinely witnesses our chain head at a point in time. (Private keys live
only on the hosts under `…/keys/`, mode 0600, never committed.)

| Chain | Key fingerprint | First anchor (Rekor logIndex) |
|---|---|---|
| GENA | `bc0f9b79f84a3974` | [1703643654](https://rekor.sigstore.dev/api/v1/log/entries?logIndex=1703643654) |
| ARA  | `26b7b14ad6af84a8` | [1703718463](https://rekor.sigstore.dev/api/v1/log/entries?logIndex=1703718463) |

## Verify an anchor

Given an anchor sidecar JSON (`anchor_<ts>.json`, written by `anchor publish`):

```bash
bijotel anchor verify anchor_<ts>.json --public-key gena_ecdsa_public.pem
# → Rekor anchor MATCH  (hash_matches / pubkey_matches / signature_valid)
```

Requires `bijotel >= 2.13.2` (the ECDSA Rekor live-interop fix; earlier
versions signed with Ed25519, which Rekor rejects).

## How it runs in production

A daily cron (`rekor_anchor.sh`, 03:30) publishes the then-current chain head
to Rekor and stores the sidecar next to the chain DB (`…/anchors/`). Any
tampering with sealed entries up to an anchored head becomes externally
detectable: the recomputed head hash would no longer match the publicly
logged one.
