"""``bijotel keygen`` — Ed25519 keypair generation (v2.1.0)."""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from pathlib import Path


def keygen_cmd(args: argparse.Namespace) -> int:
    """Generate an Ed25519 keypair for signing chain exports.

    Writes two PEM files into ``--output-dir`` (default ``./keys``):

      * ``bijotel_private.pem`` — keep secret. Used by ``bijotel export
        --sign-key``.
      * ``bijotel_public.pem``  — distribute to auditors. Used by
        ``bijotel verify-export --public-key``.

    Refuses to overwrite an existing private key file unless ``--force``
    is passed; rotating a signing key by accident is bad.
    """
    from bijotel.crypto.ed25519 import generate_keypair, public_key_fingerprint

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    priv_path = out_dir / "bijotel_private.pem"
    pub_path = out_dir / "bijotel_public.pem"

    if priv_path.exists() and not args.force:
        print(
            f"ERROR: {priv_path} already exists. Pass --force to overwrite "
            "(this rotates your signing key — old signed exports will no "
            "longer carry a matching key).",
            file=sys.stderr,
        )
        return 2

    private_pem, public_pem = generate_keypair()
    priv_path.write_bytes(private_pem)
    pub_path.write_bytes(public_pem)
    # Lock down private key permissions where the OS supports it.
    # On Windows / restricted FS the chmod is a no-op and we don't
    # treat it as fatal — the private key is still inside the user's
    # directory and the documented expectation is they protect it via
    # secrets manager in production.
    with contextlib.suppress(OSError):
        os.chmod(priv_path, 0o600)

    fp = public_key_fingerprint(public_pem)
    print(f"Generated Ed25519 keypair in {out_dir}/")
    print(f"  Private key: {priv_path}  (mode 0o600 — keep secret)")
    print(f"  Public key:  {pub_path}   (share with auditors)")
    print(f"  Fingerprint: {fp}")
    print()
    print("Next steps:")
    print(f"  bijotel export --db chain.db -o export.json --sign-key {priv_path}")
    print(
        f"  bijotel verify-export export.json --public-key {pub_path} "
        "[--secret-hex <hex>]"
    )
    return 0
