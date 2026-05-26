"""Ed25519 keypair generation, signing, and verification.

Thin wrapper over ``cryptography.hazmat.primitives.asymmetric.ed25519``.
The wrapper exists so the rest of BIJOTEL can import a stable surface
without taking a direct dependency on a specific ``cryptography``
internal path, and so signing failures are reported as ``False`` /
``ValueError`` rather than as opaque library exceptions.

Conventions
-----------
- **Private keys** are stored as PEM (PKCS#8, no encryption). Operators
  are expected to protect the file at rest via filesystem permissions
  and/or a secrets manager.
- **Public keys** travel in two forms: a PEM file the auditor receives
  out of band, and a raw-32-byte base64 string embedded in the export
  JSON itself so the file is self-describing.
- **Signatures** are raw 64-byte Ed25519 outputs, base64-encoded when
  embedded in JSON.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def generate_keypair() -> tuple[bytes, bytes]:
    """Generate a fresh Ed25519 keypair.

    Returns:
        ``(private_pem, public_pem)`` — both as PEM-encoded bytes
        suitable for writing to ``.pem`` files.
    """
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def load_private_pem(path: str | Path) -> bytes:
    """Read a PEM private key from disk, returning the raw PEM bytes.

    Caller is expected to pass the result to :func:`sign`. Errors out
    early with ``FileNotFoundError`` if the path is wrong rather than
    surfacing a vaguer error at sign time.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"private key not found: {path}")
    return path.read_bytes()


def load_public_pem(path: str | Path) -> bytes:
    """Read a PEM public key from disk, returning the raw PEM bytes."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"public key not found: {path}")
    return path.read_bytes()


def sign(data: bytes, private_pem: bytes) -> bytes:
    """Sign ``data`` with the private key. Returns the raw 64-byte signature.

    Raises:
        ValueError: if ``private_pem`` is not a valid PEM Ed25519 key.
    """
    try:
        key = serialization.load_pem_private_key(private_pem, password=None)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid private key PEM: {e}") from e
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(
            f"private key is not Ed25519 (got {type(key).__name__})"
        )
    return key.sign(data)


def verify(data: bytes, signature: bytes, public_pem: bytes) -> bool:
    """Verify ``signature`` against ``data`` under the given public key.

    Returns ``True`` if the signature is valid, ``False`` otherwise.
    All failure modes (wrong key, tampered data, malformed signature,
    malformed key) collapse to ``False`` — callers that need to
    distinguish should validate the inputs first.
    """
    try:
        key = serialization.load_pem_public_key(public_pem)
    except (ValueError, TypeError):
        return False
    if not isinstance(key, Ed25519PublicKey):
        return False
    try:
        key.verify(signature, data)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def public_key_raw_b64(public_pem: bytes) -> str:
    """Return the 32-byte raw public key, base64-encoded.

    Embedded in the export JSON so the file is self-describing. The
    auditor's own copy of the public key (received out of band) must
    match this value; the export-signature handler compares them.

    Raises:
        ValueError: if ``public_pem`` is not a valid Ed25519 public key.
    """
    try:
        key = serialization.load_pem_public_key(public_pem)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid public key PEM: {e}") from e
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(
            f"public key is not Ed25519 (got {type(key).__name__})"
        )
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def public_key_fingerprint(public_pem: bytes) -> str:
    """Short, stable fingerprint of an Ed25519 public key for display.

    Computed as SHA-256(raw public key bytes), truncated to the first
    16 hex characters. Used in CLI output ("signed by 8f0a9c4b...")
    and in error messages where the full key would be noise.
    """
    try:
        key = serialization.load_pem_public_key(public_pem)
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid public key PEM: {e}") from e
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(
            f"public key is not Ed25519 (got {type(key).__name__})"
        )
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]
