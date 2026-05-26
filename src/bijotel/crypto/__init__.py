"""Asymmetric signature primitives for chain exports.

Ed25519 signing of chain exports lets an auditor verify the integrity
of an exported chain without holding the HMAC seal-time secret. The
auditor receives the export JSON + the operator's public key (or its
fingerprint) and runs ``bijotel verify-export --public-key``; no
opportunity exists for the auditor to forge a valid chain.

The HMAC chain is untouched. Ed25519 is an additional outer layer over
the exported file, not a replacement for the per-entry HMAC linkage.
"""

from bijotel.crypto.ed25519 import (
    generate_keypair,
    load_private_pem,
    load_public_pem,
    public_key_fingerprint,
    public_key_raw_b64,
    sign,
    verify,
)

__all__ = [
    "generate_keypair",
    "load_private_pem",
    "load_public_pem",
    "public_key_fingerprint",
    "public_key_raw_b64",
    "sign",
    "verify",
]
