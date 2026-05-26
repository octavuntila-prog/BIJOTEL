"""``bijotel.anchoring`` — anchor BIJOTEL chain heads in a public
transparency log (v2.9.0).

BIJOTEL already proves "this chain has not been tampered with" via the
HMAC chain + ``bijotel verify``. What it cannot prove on its own is
**when** the chain reached a given state — only the operator knows,
and an operator could in principle backdate everything they control.

Rekor anchoring closes that gap. It records, in the public Sigstore
Rekor transparency log, that operator X attested chain head Y at
time T. Rekor is run by the Linux Foundation, is append-only, and
produces inclusion proofs against a published merkle root — so an
operator cannot retroactively change which head_hash they attested.

Two orthogonal guarantees:

    bijotel verify  : "the chain bytes have not been altered"
    rekor anchor    : "operator X attested head Y at time T (witnessed)"

Together: tamper-evidence + timestamping + public witnessing.

Public API:
    ``anchor_chain_head(db_path, *, sign_key_pem)`` — sign the current
        chain head with the operator's Ed25519 key and upload to Rekor.
        Returns a ``RekorAnchor`` with the log index, UUID, integrated
        time, and the raw signature.
    ``verify_rekor_anchor(anchor, *, expected_head_hash, expected_pubkey_pem)``
        — fetch the entry from Rekor by log index, confirm it matches
        the expected head + public key. Returns ``AnchorVerifyResult``.
    ``RekorAnchor`` / ``AnchorVerifyResult`` — frozen dataclasses with
        ``to_dict()`` for JSON.
    ``REKOR_PUBLIC_URL`` — the default endpoint (https://rekor.sigstore.dev).
"""

from __future__ import annotations

from bijotel.anchoring.rekor import (
    REKOR_PUBLIC_URL,
    AnchorVerifyResult,
    RekorAnchor,
    RekorClient,
    anchor_chain_head,
    verify_rekor_anchor,
)

__all__ = [
    "REKOR_PUBLIC_URL",
    "AnchorVerifyResult",
    "RekorAnchor",
    "RekorClient",
    "anchor_chain_head",
    "verify_rekor_anchor",
]
