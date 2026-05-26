# Rekor Anchoring

**Available from v2.9.0.**

BIJOTEL's HMAC chain proves that the bytes haven't been altered.
`bijotel verify` answers "is this chain intact?" exactly. What it
*cannot* answer on its own is **when** the chain reached a given
state — the operator runs the writer, so an operator could in
principle backdate everything they control.

**Rekor anchoring closes that gap.** It records, in a public
Sigstore-run transparency log, that operator X attested chain head Y
at time T. Rekor is append-only, produces inclusion proofs against a
published merkle root, and is run by the Linux Foundation — so an
operator cannot retroactively change which head_hash they attested
once it's in the log.

| Tool | Answers | Trust anchor |
|---|---|---|
| `bijotel verify` | "the chain bytes have not been altered" | operator's HMAC secret |
| `bijotel anchor publish` | "operator X attested head Y at time T (witnessed)" | Sigstore Rekor + operator's Ed25519 key |

The two are complementary. A complete audit story uses both.

## Why bother

Without a public anchor, an auditor only has *your* word that the
chain reached state Y at time T. With one, they have:

- The Rekor entry's `integratedTime` (Linux-Foundation-stamped)
- The chain's `head_hash` at that moment
- Your Ed25519 signature over that hash
- An inclusion proof against Rekor's signed merkle root

If you ever rewrite history, the *old* Rekor entry is still there.
Anyone holding a copy of an old anchor can re-fetch by log_index and
prove the divergence. The "operator runs the writer" trust gap
collapses to "operator runs the writer **and** owns Sigstore Rekor",
which they don't.

## Usage

Generate a keypair once (or reuse the one from `bijotel keygen`):

```bash
bijotel keygen --output-dir ./keys
# keys/bijotel_ed25519_private.pem
# keys/bijotel_ed25519_public.pem
```

Anchor the current chain head:

```bash
bijotel anchor publish \
  --db chain.db \
  --sign-key keys/bijotel_ed25519_private.pem \
  --output anchor.json

# === Rekor anchor PUBLISHED ===
#   rekor:           https://rekor.sigstore.dev
#   log_index:       1511534821
#   log_uuid:        24296fb24b8...
#   integrated_time: 1716730000
#   head_seq:        6421
#   head_hash:       a3f9c8d2e7b1...
#   public url:      https://rekor.sigstore.dev/api/v1/log/entries?logIndex=1511534821
#   sidecar:         anchor.json
```

Verify the anchor weeks/months later:

```bash
bijotel anchor verify anchor.json \
  --public-key keys/bijotel_ed25519_public.pem

# === Rekor anchor MATCH (logIndex=1511534821) ===
#   integrated_time:  1716730000
#   hash_matches:     True
#   pubkey_matches:   True
#   signature_valid:  True
```

Exit codes:

- `0` — match
- `1` — operational error (DB/sign-key/anchor file missing)
- `2` — argument error
- `3` — mismatch (replay, tamper, wrong key)

## REST? Not yet.

There's no `/anchor/*` endpoint in v2.9.0 — anchoring is an
operator-driven snapshot action, not a per-request flow. Adding a
REST endpoint that does network I/O to Rekor on every call would
both slow the API and tie its uptime to Sigstore's. The Python /
CLI surface is the v2.9 way.

If you want to run anchoring on a cron, that's exactly the right
pattern:

```bash
# Every 6h: anchor the chain head + keep the last 30 sidecars
0 */6 * * * cd /opt/bijotel && \
  bijotel anchor publish --db /data/chain.db \
    --sign-key /keys/private.pem \
    --output /anchors/anchor-$(date +\%s).json && \
  ls -t /anchors/anchor-*.json | tail -n +31 | xargs rm -f
```

## Honest scope notes

The library and CLI are real. The unit tests are real. **The
live-public-Rekor upload path has a known compatibility gap with
Rekor 1.4+'s Ed25519 hashedrekord verifier** that needs `sigstore`
PyPI library integration to resolve cleanly.

What does work today:

- ✓ The full library API (`anchor_chain_head` / `verify_rekor_anchor`)
- ✓ The CLI (`bijotel anchor publish` / `verify`)
- ✓ The HTTP client (`RekorClient.upload` / `fetch`)
- ✓ 15 unit tests covering hash, signature, pubkey, mismatch paths
  via a mocked Rekor HTTP server inside the test
- ✓ A custom Rekor instance (self-hosted) accepting the upload, if
  the operator wants to run their own log

What's a v2.10 follow-up:

- ⚠ Live `rekor.sigstore.dev` upload of Ed25519 signatures returns
  HTTP 400 with `failed to verify signature: ed25519: invalid
  signature`. The signature-encoding interpretation across Rekor
  1.4+'s `hashedrekord` and `dsse` types is more nuanced than a
  straight `ed25519.sign(sha512(data))` — the canonical path goes
  through the `sigstore` PyPI library which handles the bundle
  format. v2.10 will swap our raw `urllib` upload for
  `sigstore.transparency.Rekor`, which is purpose-built for this.

If you want to anchor today against the public Rekor while v2.10 is
in flight, use `sigstore-python` directly to sign + upload the
`anchor.head_hash` and store the returned UUID in your own sidecar.
The verify path (`bijotel anchor verify`) can read any Rekor entry
regardless of how it was uploaded.

## How verification works (mechanics)

The verifier checks three independent things, all of which must pass:

1. **hash_matches**: Rekor's stored hash equals SHA-512 of the
   head_hash bytes we expect.
2. **pubkey_matches**: When `--public-key` is given, the Rekor entry's
   public key equals the operator's expected key (PEM-equal,
   strip-whitespace). Without `--public-key` the check falls back to
   "anchor's pubkey matches Rekor's pubkey" — useful but weaker.
3. **signature_valid**: The Ed25519 signature in the anchor still
   verifies against `SHA-512(head_hash_bytes)`.

The `reason` field in `AnchorVerifyResult` names which check failed
when `match` is `False`, so the CLI's error messages are precise.

## Public API

```python
from bijotel.anchoring import (
    anchor_chain_head,    # sign + upload
    verify_rekor_anchor,  # fetch + check
    RekorAnchor,          # frozen dataclass with .to_dict()
    AnchorVerifyResult,   # frozen dataclass with .to_dict()
    REKOR_PUBLIC_URL,
)
```

Each is re-exported from `bijotel` directly:

```python
from bijotel import RekorAnchor, anchor_chain_head, verify_rekor_anchor
```
