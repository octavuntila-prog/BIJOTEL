# Your First Verification

The strongest demo BIJOTEL can give is **letting you verify a real
chain yourself**. Same code path real auditors use. No screenshots,
no mocks, no "trust me".

## The verify-yourself demo

We host a public 200-entry chain at
[bijotel.whiteandpoint.com](https://bijotel.whiteandpoint.com/), built
with `bijotel` 2.0.5, exported via `bijotel export`. The HMAC secret is
intentionally public so visitors can verify.

### Step 1 — Install BIJOTEL

```bash
pip install bijotel
```

### Step 2 — Download the demo chain

```bash
curl -O https://bijotel.whiteandpoint.com/demo_chain.json
```

You now have a 302 KB JSON archive: 200 OpenTelemetry GenAI spans
(mix of Anthropic Haiku/Sonnet + OpenAI gpt-4o-mini/gpt-4o),
distributed across the past 14 days, with ~8% F11 attack-pattern
prompts mixed in.

### Step 3 — Verify integrity

```bash
bijotel verify-export demo_chain.json \
  --secret-hex bd1ed00aded0bd1ed00aded0bd1ed00aded0bd1ed00aded0bd1ed00aded00000
```

Expected output:

```
Export VALID: demo_chain.json
```

Exit code: `0`.

## Now try a tampered version

We pre-built a variant with **exactly one byte flipped** at entry
seq=100. The HMAC chain links are still intact at the chain-hash
level — but the v2.0.3 canonical_body integrity check catches it.

```bash
curl -O https://bijotel.whiteandpoint.com/demo_chain_tampered.json

bijotel verify-export demo_chain_tampered.json \
  --secret-hex bd1ed00aded0bd1ed00aded0bd1ed00aded0bd1ed00aded0bd1ed00aded00000
```

Expected output (on stderr):

```
Export INVALID: canonical_body tampered at seq=100:
  body hashes to c1711163a58a53ac... but canonical_hash claims 273564cf0fc9e063...
```

Exit code: `1`.

## What just happened

BIJOTEL verified every entry by:

1. **Re-computing SHA-256** of each canonical body
   (RFC 8785 JCS-canonicalized JSON).
2. **Re-computing HMAC-SHA256**: `hmac(prev_hash || canonical_hash, secret)`.
3. **Checking each entry's `prev_hash`** matches the previous entry's
   `hmac_hash` (or GENESIS for seq=1).
4. **v2.0.3+**: also re-hashes `canonical_body_b64` and compares against
   stored `canonical_hash` — catches body mutations even when the chain
   links themselves haven't been touched.

One changed byte at seq=100 → the SHA-256 of that body diverges from
the stored `canonical_hash` → exact entry surfaced, with both hashes
in the error message.

!!! note "CLI failure signaling"
    `bijotel verify` and `bijotel verify-export` write failure reasons
    to **stderr** and exit with code **3** (chain) or **1**
    (export). If you script the CLI, branch on exit code, not stdout
    substring match.

## Try it on your own chain

```bash
# Export your chain
bijotel export --db chain.db -o my_chain.json

# Verify with the secret it was sealed under
BIJOTEL_HMAC_SECRET=$YOUR_SECRET bijotel verify-export my_chain.json
```

The format is `bijotel-chain-v1` — versioned for forward compatibility.
Auditors ship the JSON; you ship them the secret (out of band).

## Next

- [Policy Engine](../guides/policy-engine.md) — pre-call gating
- [Multi-Provider](../guides/multi-provider.md) — Anthropic + OpenAI + xAI in one chain
- [CLI Reference](../api/cli.md) — every command
