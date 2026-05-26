"""Unit tests for ``bijotel.crypto.ed25519`` (v2.1.0).

These tests cover the thin wrapper around ``cryptography``'s Ed25519
primitives. End-to-end signed-export tests live in
``test_export_signed.py``.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from bijotel.crypto.ed25519 import (
    generate_keypair,
    load_private_pem,
    load_public_pem,
    public_key_fingerprint,
    public_key_raw_b64,
    sign,
    verify,
)


def test_generate_keypair_returns_two_pems() -> None:
    """generate_keypair() returns (private_pem, public_pem) as bytes."""
    priv, pub = generate_keypair()
    assert isinstance(priv, bytes)
    assert isinstance(pub, bytes)
    assert priv.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert pub.startswith(b"-----BEGIN PUBLIC KEY-----")
    # Two independent calls produce different keypairs.
    priv2, pub2 = generate_keypair()
    assert priv2 != priv
    assert pub2 != pub


def test_sign_then_verify_passes() -> None:
    """Sign with private, verify with corresponding public — True."""
    priv, pub = generate_keypair()
    data = b"hello bijotel ed25519"
    sig = sign(data, priv)
    assert isinstance(sig, bytes)
    assert len(sig) == 64  # Ed25519 signatures are 64 bytes
    assert verify(data, sig, pub) is True


def test_verify_wrong_key_fails() -> None:
    """Signature from key A does NOT verify under key B's public part."""
    priv_a, _pub_a = generate_keypair()
    _priv_b, pub_b = generate_keypair()
    sig = sign(b"some content", priv_a)
    assert verify(b"some content", sig, pub_b) is False


def test_verify_tampered_data_fails() -> None:
    """Mutating any byte of the signed data breaks verification."""
    priv, pub = generate_keypair()
    data = b"original content"
    sig = sign(data, priv)
    tampered = b"original Content"  # one byte difference
    assert verify(tampered, sig, pub) is False


def test_verify_malformed_signature_returns_false() -> None:
    """A 63-byte signature (truncated) verifies as False, not an exception."""
    _priv, pub = generate_keypair()
    assert verify(b"data", b"\x00" * 63, pub) is False
    assert verify(b"data", b"", pub) is False
    assert verify(b"data", b"not a real sig", pub) is False


def test_verify_malformed_pubkey_returns_false() -> None:
    """A bad PEM blob never raises — just returns False."""
    priv, _pub = generate_keypair()
    sig = sign(b"data", priv)
    assert verify(b"data", sig, b"not a PEM key") is False
    assert verify(b"data", sig, b"") is False


def test_sign_with_invalid_private_pem_raises() -> None:
    """Garbage PEM raises ValueError instead of returning silently."""
    with pytest.raises(ValueError):
        sign(b"data", b"-----BEGIN PRIVATE KEY-----\ngarbage\n-----END PRIVATE KEY-----\n")
    with pytest.raises(ValueError):
        sign(b"data", b"not a pem at all")


def test_public_key_raw_b64_is_32_bytes() -> None:
    """The raw public key is exactly 32 bytes (Ed25519 spec)."""
    _priv, pub = generate_keypair()
    b64 = public_key_raw_b64(pub)
    assert isinstance(b64, str)
    raw = base64.b64decode(b64)
    assert len(raw) == 32


def test_public_key_raw_b64_invalid_input_raises() -> None:
    with pytest.raises(ValueError):
        public_key_raw_b64(b"not a pem")


def test_public_key_fingerprint_is_16_hex_chars() -> None:
    """Fingerprint is first 16 chars of SHA-256(raw 32-byte pubkey)."""
    _priv, pub = generate_keypair()
    fp = public_key_fingerprint(pub)
    assert isinstance(fp, str)
    assert len(fp) == 16
    int(fp, 16)  # raises if not hex


def test_public_key_fingerprint_stable_for_same_key() -> None:
    """Computing the fingerprint twice on the same PEM gives the same value."""
    _priv, pub = generate_keypair()
    assert public_key_fingerprint(pub) == public_key_fingerprint(pub)


def test_public_key_fingerprint_different_for_different_keys() -> None:
    _priv1, pub1 = generate_keypair()
    _priv2, pub2 = generate_keypair()
    assert public_key_fingerprint(pub1) != public_key_fingerprint(pub2)


def test_load_private_pem_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_private_pem(tmp_path / "nonexistent.pem")


def test_load_public_pem_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_public_pem(tmp_path / "nonexistent.pem")


def test_load_private_then_use(tmp_path: Path) -> None:
    """Write a keypair to disk, reload, sign+verify — full roundtrip.

    Uses distinct names (not case-only differences) — Windows treats
    paths case-insensitively, so `p.pem` vs `P.pem` would alias and
    the second write would silently overwrite the first.
    """
    priv, pub = generate_keypair()
    priv_path = tmp_path / "private.pem"
    pub_path = tmp_path / "public.pem"
    priv_path.write_bytes(priv)
    pub_path.write_bytes(pub)

    loaded_priv = load_private_pem(priv_path)
    loaded_pub = load_public_pem(pub_path)
    sig = sign(b"reloaded test", loaded_priv)
    assert verify(b"reloaded test", sig, loaded_pub) is True
