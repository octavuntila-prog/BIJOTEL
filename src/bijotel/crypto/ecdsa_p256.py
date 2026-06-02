"""ECDSA P-256 signing for Rekor transparency-log anchoring.

Why a second algorithm alongside Ed25519: Rekor's ``hashedrekord`` verifies
Ed25519 entries via **Ed25519ph** (pre-hashed EdDSA, RFC 8032), which Python's
``cryptography`` cannot emit (``Ed25519PrivateKey.sign`` is pure EdDSA only).
ECDSA P-256 over a SHA-256 digest is Rekor's canonical, natively-supported
path. So chain-head **anchors** are signed with ECDSA P-256, while signed
chain **exports** keep Ed25519 (:mod:`bijotel.crypto.ed25519`).

Conventions mirror ``ed25519.py``: PEM private (PKCS#8, unencrypted), PEM
public (SubjectPublicKeyInfo); anchor signatures are DER-encoded ECDSA — the
form Rekor accepts on the wire.

Provenance: BIJOTEL-original, v2.13.2 (Rekor live-interop fix).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


def generate_keypair() -> tuple[bytes, bytes]:
    """Generate a fresh ECDSA P-256 (secp256r1) keypair.

    Returns ``(private_pem, public_pem)`` as PEM-encoded bytes.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def load_private_pem(path: str | Path) -> bytes:
    """Read a PEM private key from disk (raw bytes); error early if missing."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"private key not found: {path}")
    return path.read_bytes()


def load_public_pem(path: str | Path) -> bytes:
    """Read a PEM public key from disk (raw bytes)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"public key not found: {path}")
    return path.read_bytes()


def sign_digest(data: bytes, private_pem: bytes) -> bytes:
    """ECDSA-P256 signature over SHA-256(``data``). Returns DER-encoded bytes.

    ``cryptography`` hashes ``data`` with SHA-256 internally and signs that
    digest — exactly what Rekor's sha256 hashedrekord verifies against.

    Raises:
        ValueError: if ``private_pem`` is not a valid EC private key (e.g. an
            Ed25519 key was passed — Rekor cannot verify those).
    """
    try:
        key = serialization.load_pem_private_key(private_pem, password=None)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid private key PEM: {e}") from e
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError(
            f"anchor signing requires an ECDSA P-256 key, got "
            f"{type(key).__name__}. Generate one with: bijotel keygen --type ecdsa"
        )
    return key.sign(data, ec.ECDSA(hashes.SHA256()))


def verify_digest(data: bytes, signature: bytes, public_pem: bytes) -> bool:
    """Verify a DER ECDSA-P256 signature over SHA-256(``data``). True/False."""
    try:
        key = serialization.load_pem_public_key(public_pem)
    except (ValueError, TypeError):
        return False
    if not isinstance(key, ec.EllipticCurvePublicKey):
        return False
    try:
        key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def public_key_fingerprint(public_pem: bytes) -> str:
    """Short, stable fingerprint of an EC public key: SHA-256(point)[:16]."""
    try:
        key = serialization.load_pem_public_key(public_pem)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid public key PEM: {e}") from e
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError(f"public key is not ECDSA (got {type(key).__name__})")
    raw = key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return hashlib.sha256(raw).hexdigest()[:16]
