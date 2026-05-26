"""Receipt dataclasses + local verification helper for federation.

All three receipt shapes are frozen dataclasses with ``to_dict()`` for
JSON serialization. Operators save these as sidecar files (operator
vault, S3, etc.) so an auditor can later replay verification without
the federation service being online.

The local verify path (`verify_cross_anchor_receipt`) covers the most
audit-critical question: "does the federation's signature on this
receipt actually verify?" It does *not* go to Rekor — that's an
optional second leg the auditor can perform separately if they want
the publicly-witnessed timestamp confirmed.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from bijotel.crypto.ed25519 import verify as ed25519_verify


@dataclass(frozen=True)
class RegistrationReceipt:
    """Returned by ``POST /register``.

    Persist this — the ``operator_id`` is needed for every subsequent
    submission, and the ``rekor_log_index`` is the public-witness
    proof that the registration happened at a specific time.
    """

    operator_id: str
    org_name: str
    public_key_pem: str
    registered_at: str
    rekor_log_index: int
    federation_url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SubmissionReceipt:
    """Returned by ``POST /submit``.

    Each submission gets a receipt with the federation's confirmation
    that the export was structurally valid (Ed25519 signature on the
    envelope verified, continuity check passed). The cross-anchor
    happens later; ``pending_anchor=True`` means "your submission is
    in the queue".
    """

    submission_id: str
    operator_id: str
    verified: bool
    entry_count: int
    first_seq: int
    last_seq: int
    continuity_verified: bool
    submitted_at: str
    pending_anchor: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CrossAnchorReceipt:
    """The artefact auditors verify against.

    Carries everything needed for a local verify (per
    ``verify_cross_anchor_receipt``):

      * The cross-anchor hash (recomputable from participating
        operators' chain_signatures).
      * The list of participating operators (just their IDs +
        snapshot of the chain_signature they contributed).
      * The federation's signature over the canonical receipt payload.
      * A snapshot of the federation's public key at signing time
        (so post-rotation receipts still verify).
      * Rekor coordinates for the public-witness leg.
    """

    anchor_id: str
    cross_anchor_hash: str
    participating_operators: list[dict[str, str]]  # [{operator_id, chain_signature}]
    anchored_at: str
    rekor_log_index: int | None
    rekor_url: str | None
    federation_signature: str
    federation_public_key_pem: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------
# Local verification
# ---------------------------------------------------------------------


def _canonical_receipt_payload(receipt: CrossAnchorReceipt) -> bytes:
    """The bytes the federation signed.

    Sorted-key compact JSON of the receipt minus the signature itself.
    Deterministic across Python versions so re-signing produces
    byte-identical output. The federation's reference implementation
    must build the same shape.
    """
    payload_dict = {
        "anchor_id": receipt.anchor_id,
        "anchored_at": receipt.anchored_at,
        "cross_anchor_hash": receipt.cross_anchor_hash,
        "federation_public_key_pem": receipt.federation_public_key_pem,
        "participating_operators": receipt.participating_operators,
        "rekor_log_index": receipt.rekor_log_index,
        "rekor_url": receipt.rekor_url,
    }
    return json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def verify_cross_anchor_receipt(
    receipt: CrossAnchorReceipt,
    *,
    federation_public_key_pem: bytes | str | None = None,
) -> dict[str, Any]:
    """Verify the federation signature on a cross-anchor receipt.

    Args:
        receipt: A ``CrossAnchorReceipt`` (typically loaded from a
            sidecar JSON file).
        federation_public_key_pem: Optional. The federation's expected
            public key (PEM bytes or str). If omitted, the check uses
            the public key embedded in the receipt — useful for sanity
            checks, but a real auditor binds against an out-of-band
            copy of the federation's public key.

    Returns:
        ``{"valid": bool, "checks": {...}, "reason": str|None}`` — keep
        the shape symmetric with ``verify_rekor_anchor`` so CLI
        callers can render uniformly.
    """
    # If caller supplied an expected key, it must match the snapshot.
    if federation_public_key_pem is not None:
        if isinstance(federation_public_key_pem, str):
            expected = federation_public_key_pem.encode("ascii")
        else:
            expected = federation_public_key_pem
        if expected.decode("ascii").strip() != receipt.federation_public_key_pem.strip():
            return {
                "valid": False,
                "checks": {
                    "pubkey_matches_expected": False,
                    "signature_verified": False,
                    "cross_anchor_hash_recomputed": False,
                },
                "reason": (
                    "federation public key in receipt does not match the "
                    "expected key supplied by the verifier"
                ),
            }

    # Recompute the cross-anchor hash from participating operators'
    # chain_signatures + anchored_at. Federation must use the same
    # formula (sorted by operator_id, concatenated, ts appended).
    sorted_ops = sorted(
        receipt.participating_operators, key=lambda o: o.get("operator_id", "")
    )
    blob = "".join(o["chain_signature"] for o in sorted_ops).encode("utf-8")
    # The federation appends the timestamp_ns; we reconstruct from
    # anchored_at. Operators and federations MUST agree on this
    # serialization — recommended canonical form is ISO-8601 with the
    # Z suffix. (Federation reference impl in v3 will lock the exact
    # bytes; for v2.11 the receipt is the source of truth.)
    blob += receipt.anchored_at.encode("utf-8")
    recomputed = hashlib.sha256(blob).hexdigest()
    hash_matches = recomputed == receipt.cross_anchor_hash

    # Verify federation Ed25519 signature over the canonical payload.
    try:
        sig = base64.b64decode(receipt.federation_signature)
        payload = _canonical_receipt_payload(receipt)
        pubkey = receipt.federation_public_key_pem.encode("ascii")
        sig_ok = bool(ed25519_verify(payload, sig, pubkey))
    except Exception:
        sig_ok = False

    valid = hash_matches and sig_ok
    reason = None
    if not valid:
        bits = []
        if not hash_matches:
            bits.append(
                "cross-anchor hash mismatch — receipt's stored hash "
                "does not match recomputation from participating "
                "operators (someone edited the participant list?)"
            )
        if not sig_ok:
            bits.append(
                "federation Ed25519 signature verification failed — "
                "receipt was tampered with after signing, or the "
                "wrong federation public key is in the snapshot"
            )
        reason = "; ".join(bits)

    return {
        "valid": valid,
        "checks": {
            "pubkey_matches_expected": (
                True if federation_public_key_pem is None else True
            ),
            "signature_verified": sig_ok,
            "cross_anchor_hash_recomputed": hash_matches,
        },
        "reason": reason,
    }
