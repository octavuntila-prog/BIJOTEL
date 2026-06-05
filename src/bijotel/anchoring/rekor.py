"""Rekor (Sigstore) transparency-log client for BIJOTEL chain anchoring.

Implementation notes:

  * HTTP via stdlib ``urllib`` only — no new runtime dependency.
  * Entry type: ``hashedrekord/0.0.1``. We sign the raw 32 bytes of the
    chain's head_hash (decoded from the 64-hex string), upload
    (signature, public key, SHA-256(data)) to Rekor, and persist the
    returned log_index + UUID + integrated_time.
  * Verify path: pull the entry by log_index, parse the body, confirm
    (a) the hash matches what we recompute locally, (b) the public key
    matches the operator's known PEM, (c) the signature still verifies
    against the data.
  * Inclusion-proof verification (against Rekor's signed merkle root)
    is out of scope for v2.9.0 — the use case here is "did operator X
    publish this attestation?" which the per-entry fetch already
    answers. Full STH+proof verification is a v2.10 follow-up.

Limits acknowledged in the module docstring of ``bijotel.anchoring``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bijotel.crypto.ecdsa_p256 import (
    load_private_pem,
    load_public_pem,
    sign_digest,
    verify_digest,
)

#: The public Rekor instance run by the Linux Foundation / Sigstore.
#: Override via ``rekor_url=`` to point at a private Rekor for
#: air-gapped or higher-trust deployments.
REKOR_PUBLIC_URL = "https://rekor.sigstore.dev"


# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------


class RekorEntryExistsError(RuntimeError):
    """Raised by :meth:`RekorClient.upload` on HTTP 409 (Conflict).

    Rekor returns 409 when an *equivalent* entry already exists in the
    transparency log — the same (data hash, signature, public key) triple
    was uploaded before. For BIJOTEL this happens when a chain head is
    re-anchored without having advanced (an idle gap between two daily
    anchor runs). The existing entry already witnesses that head, so this
    is an idempotent no-op, not a failure — callers should treat it as
    success. Subclasses :class:`RuntimeError` so existing
    ``except RuntimeError`` handlers still catch it as a safe fallback.
    """


# ---------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class RekorAnchor:
    """One Rekor anchor for a BIJOTEL chain head.

    Persist this (sidecar JSON or operator vault) so the chain can be
    publicly verified later. Without the ``log_index`` + ``log_uuid`` an
    auditor cannot find the entry in Rekor's billion-entry log.
    """

    rekor_url: str          # the Rekor instance used (e.g. https://rekor.sigstore.dev)
    log_index: int          # global Rekor log index (1.5B+ at time of writing)
    log_uuid: str           # per-entry UUID (also accepted by GET /entries)
    integrated_time: int    # unix seconds when Rekor stamped the entry
    head_hash: str          # the BIJOTEL chain head_hash (hex, 64 chars)
    head_seq: int           # the chain seq when this anchor was created
    signature_b64: str      # Ed25519 signature over bytes.fromhex(head_hash)
    public_key_pem: str     # operator's Ed25519 public PEM (so verify is self-contained)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnchorVerifyResult:
    """Outcome of ``verify_rekor_anchor``.

    ``match`` is the headline. The other fields surface *why* the
    answer is what it is, so callers (CLI / REST) can produce specific
    error messages.
    """

    match: bool
    log_index: int
    integrated_time: int | None     # echo of Rekor's stamp; None if entry not found
    hash_matches: bool              # did Rekor's hash == SHA256(head_hash bytes)?
    pubkey_matches: bool            # did Rekor's pubkey == expected operator PEM?
    signature_valid: bool           # did the Ed25519 verify pass over the head_hash?
    reason: str | None = None       # human-readable explanation when match=False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------


class RekorClient:
    """Minimal Rekor HTTP client (stdlib urllib).

    Two methods: ``upload`` (POST a hashedrekord entry) and ``fetch``
    (GET an entry by log_index).

    Constructed with the Rekor base URL — defaults to the public
    instance. Pass a different URL for a private Rekor deployment.
    """

    def __init__(self, base_url: str = REKOR_PUBLIC_URL, *, timeout: float = 30.0):
        # Strip trailing slash so URL joining is unambiguous.
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def upload(
        self,
        *,
        data_hash_hex: str,
        signature_b64: str,
        public_key_pem_b64: str,
    ) -> dict[str, Any]:
        """POST a hashedrekord entry to Rekor.

        Returns the parsed JSON response, which is a single-key dict
        whose key is the entry UUID and whose value carries
        ``logIndex``, ``integratedTime``, ``body``, etc.
        """
        payload = {
            "apiVersion": "0.0.1",
            "kind": "hashedrekord",
            "spec": {
                "data": {
                    # ECDSA P-256 anchors use SHA-256 — Rekor's canonical
                    # hashedrekord digest for EC keys. (Ed25519 is NOT used:
                    # Rekor verifies it via Ed25519ph, which Python's
                    # cryptography cannot emit. See bijotel.crypto.ecdsa_p256.)
                    "hash": {"algorithm": "sha256", "value": data_hash_hex},
                },
                "signature": {
                    "content": signature_b64,
                    "publicKey": {"content": public_key_pem_b64},
                },
            },
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/log/entries",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # Rekor includes a JSON error body — surface it for triage.
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 409:
                # Equivalent entry already exists — idempotent re-anchor of an
                # unchanged head. Raise a typed error so callers can treat it
                # as "already anchored" (success) rather than a hard failure.
                raise RekorEntryExistsError(
                    f"Rekor entry already exists (HTTP 409): {detail[:400]}"
                ) from e
            raise RuntimeError(
                f"Rekor upload HTTP {e.code}: {detail[:400]}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Rekor upload network error: {e}") from e

    def fetch(self, log_index: int) -> dict[str, Any]:
        """GET an entry by global log index.

        Returns the parsed JSON: ``{<uuid>: {body, logIndex, integratedTime, ...}}``.
        Raises ``RuntimeError`` if the entry is missing or Rekor errors.
        """
        url = f"{self.base_url}/api/v1/log/entries?logIndex={int(log_index)}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(
                    f"Rekor entry with logIndex={log_index} not found"
                ) from e
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Rekor fetch HTTP {e.code}: {detail[:400]}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Rekor fetch network error: {e}") from e


# ---------------------------------------------------------------------
# Anchor / verify entry points
# ---------------------------------------------------------------------


def _read_chain_head(db_path: Path) -> tuple[int, str]:
    """Return (last_seq, hmac_hash_of_last_row) for the chain at db_path."""
    if not db_path.exists():
        raise FileNotFoundError(f"chain DB not found: {db_path}")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT seq, hmac_hash FROM chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise RuntimeError(f"chain DB is empty: {db_path}")
        return int(row[0]), str(row[1])


def anchor_chain_head(
    db_path: str | Path,
    *,
    sign_key_pem: bytes | str | Path,
    rekor_url: str = REKOR_PUBLIC_URL,
) -> RekorAnchor:
    """Sign the chain head and upload the anchor to Rekor.

    Args:
        db_path: Path to the BIJOTEL chain SQLite file.
        sign_key_pem: Either the PEM bytes of the operator's Ed25519
            private key, or a path to a PEM file. Generated via
            ``bijotel keygen``.
        rekor_url: Rekor instance to publish to. Defaults to the
            public Sigstore Rekor.

    Returns:
        ``RekorAnchor`` populated with the Rekor entry's log_index,
        UUID, integrated_time, the head_hash that was anchored, and
        the signature + public-key PEM for self-contained verify.

    Raises:
        FileNotFoundError: chain DB missing.
        RuntimeError: chain empty, Rekor network/HTTP error, or
            malformed Rekor response.
    """
    db_path = Path(db_path)
    last_seq, head_hash = _read_chain_head(db_path)

    # Normalise key input: bytes pass through; path-likes get loaded.
    if isinstance(sign_key_pem, (str, Path)) and not (
        isinstance(sign_key_pem, str) and sign_key_pem.startswith("-----BEGIN")
    ):
        private_pem = load_private_pem(sign_key_pem)
    elif isinstance(sign_key_pem, str):
        private_pem = sign_key_pem.encode("utf-8")
    else:
        private_pem = sign_key_pem

    # Rekor's hashedrekord verifies an ECDSA P-256 entry by ECDSA-verifying
    # the signature against the SHA-256 digest carried in data.hash.value.
    # cryptography's ECDSA(SHA256) hashes head_bytes -> SHA-256 -> signs that
    # digest, so signing head_bytes and uploading SHA-256(head_bytes) line up.
    head_bytes = bytes.fromhex(head_hash)
    data_hash = hashlib.sha256(head_bytes).hexdigest()  # for the hash.value field
    signature_bytes = sign_digest(head_bytes, private_pem)  # DER ECDSA over SHA-256
    signature_b64 = base64.b64encode(signature_bytes).decode("ascii")

    # Derive the public PEM from the private PEM and base64-encode the
    # PEM bytes (Rekor's wire format). We don't ship the public key
    # separately because the private PEM already carries it.
    public_pem = _derive_public_pem(private_pem)
    public_pem_b64 = base64.b64encode(public_pem).decode("ascii")

    client = RekorClient(rekor_url)
    resp = client.upload(
        data_hash_hex=data_hash,
        signature_b64=signature_b64,
        public_key_pem_b64=public_pem_b64,
    )

    # Response shape: {<uuid>: {logIndex, integratedTime, body, ...}}
    if not isinstance(resp, dict) or not resp:
        raise RuntimeError(f"Rekor returned unexpected payload: {resp!r}")
    uuid, entry = next(iter(resp.items()))
    return RekorAnchor(
        rekor_url=rekor_url,
        log_index=int(entry.get("logIndex", -1)),
        log_uuid=str(uuid),
        integrated_time=int(entry.get("integratedTime", 0)),
        head_hash=head_hash,
        head_seq=last_seq,
        signature_b64=signature_b64,
        public_key_pem=public_pem.decode("ascii"),
    )


def verify_rekor_anchor(
    anchor: RekorAnchor,
    *,
    expected_public_key_pem: bytes | str | Path | None = None,
) -> AnchorVerifyResult:
    """Fetch the Rekor entry by log_index and check it matches the anchor.

    Three independent checks; all must pass for ``match=True``:

      1. ``hash_matches``: Rekor's stored hash equals SHA-256 of the
         head_hash bytes we expect to have anchored.
      2. ``pubkey_matches``: if the caller passes
         ``expected_public_key_pem``, it must equal the public key
         Rekor has on file. (Without this arg, the check is "the
         anchor's pubkey matches Rekor's pubkey" — useful but weaker.)
      3. ``signature_valid``: the Ed25519 signature in the anchor still
         verifies against ``bytes.fromhex(anchor.head_hash)``.

    Args:
        anchor: A ``RekorAnchor`` produced earlier by
            ``anchor_chain_head``.
        expected_public_key_pem: Optional operator public key (PEM
            bytes, str starting with ``-----BEGIN``, or path) to bind
            the check to. Strongly recommended in audit settings — the
            anchor is self-contained but a tampered anchor could carry
            a wrong key + matching wrong signature, so providing the
            *external* expected key is the trust-anchor.
    """
    client = RekorClient(anchor.rekor_url)

    try:
        resp = client.fetch(anchor.log_index)
    except RuntimeError as e:
        return AnchorVerifyResult(
            match=False,
            log_index=anchor.log_index,
            integrated_time=None,
            hash_matches=False,
            pubkey_matches=False,
            signature_valid=False,
            reason=f"Rekor fetch failed: {e}",
        )

    if not isinstance(resp, dict) or not resp:
        return AnchorVerifyResult(
            match=False,
            log_index=anchor.log_index,
            integrated_time=None,
            hash_matches=False,
            pubkey_matches=False,
            signature_valid=False,
            reason=f"Rekor returned unexpected payload: {resp!r}",
        )

    _, entry = next(iter(resp.items()))
    body_b64 = entry.get("body", "")
    integrated_time = int(entry.get("integratedTime", 0))

    try:
        body_json = json.loads(base64.b64decode(body_b64))
    except (ValueError, base64.binascii.Error) as e:
        return AnchorVerifyResult(
            match=False,
            log_index=anchor.log_index,
            integrated_time=integrated_time,
            hash_matches=False,
            pubkey_matches=False,
            signature_valid=False,
            reason=f"Rekor body decode failed: {e}",
        )

    spec = body_json.get("spec", {})
    rekor_hash = spec.get("data", {}).get("hash", {}).get("value", "")
    rekor_pubkey_b64 = (
        spec.get("signature", {}).get("publicKey", {}).get("content", "")
    )

    # Check 1: hash matches what we recompute from the head_hash.
    # ECDSA P-256 anchors use SHA-256 (see comment in upload()).
    head_bytes = bytes.fromhex(anchor.head_hash)
    expected_data_hash = hashlib.sha256(head_bytes).hexdigest()
    hash_matches = (rekor_hash == expected_data_hash)

    # Check 2: pubkey matches.
    try:
        rekor_pubkey_pem = base64.b64decode(rekor_pubkey_b64).decode("ascii")
    except (ValueError, base64.binascii.Error, UnicodeDecodeError):
        rekor_pubkey_pem = ""

    if expected_public_key_pem is not None:
        # Caller-supplied trust anchor — strongest check.
        if isinstance(expected_public_key_pem, (Path,)) or (
            isinstance(expected_public_key_pem, str)
            and not expected_public_key_pem.startswith("-----BEGIN")
        ):
            expected_pem_bytes = load_public_pem(expected_public_key_pem)
        elif isinstance(expected_public_key_pem, str):
            expected_pem_bytes = expected_public_key_pem.encode("ascii")
        else:
            expected_pem_bytes = expected_public_key_pem
        expected_pem_str = expected_pem_bytes.decode("ascii").strip()
        pubkey_matches = (rekor_pubkey_pem.strip() == expected_pem_str)
    else:
        # Weaker self-contained check: anchor's pubkey matches Rekor's.
        pubkey_matches = (rekor_pubkey_pem.strip() == anchor.public_key_pem.strip())

    # Check 3: the ECDSA P-256 signature verifies over SHA-256(head_bytes)
    # (the same digest Rekor verifies — see the upload-path comment above).
    try:
        sig_bytes = base64.b64decode(anchor.signature_b64)
        pubkey_for_verify = (
            anchor.public_key_pem.encode("ascii")
            if expected_public_key_pem is None
            else expected_pem_bytes  # type: ignore[possibly-undefined]
        )
        signature_valid = verify_digest(head_bytes, sig_bytes, pubkey_for_verify)
    except Exception:
        signature_valid = False

    match = hash_matches and pubkey_matches and signature_valid
    if match:
        reason = None
    else:
        bits = []
        if not hash_matches:
            bits.append(
                f"hash mismatch (rekor={rekor_hash[:16]}.., "
                f"expected={expected_data_hash[:16]}..)"
            )
        if not pubkey_matches:
            bits.append("public key mismatch")
        if not signature_valid:
            bits.append("Ed25519 signature failed verify")
        reason = "; ".join(bits) or "unknown verification failure"

    return AnchorVerifyResult(
        match=match,
        log_index=anchor.log_index,
        integrated_time=integrated_time,
        hash_matches=hash_matches,
        pubkey_matches=pubkey_matches,
        signature_valid=signature_valid,
        reason=reason,
    )


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _derive_public_pem(private_pem: bytes) -> bytes:
    """Derive the Ed25519 public-key PEM from a private-key PEM.

    Wraps cryptography's primitive so the rest of this module doesn't
    import the heavy library directly. Returns the PEM in
    SubjectPublicKeyInfo form (the format Rekor accepts).
    """
    from cryptography.hazmat.primitives import serialization

    priv = serialization.load_pem_private_key(private_pem, password=None)
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
