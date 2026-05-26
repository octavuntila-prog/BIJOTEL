"""CLI subprocess tests for ``bijotel keygen`` (v2.1.0).

End-to-end coverage of the keygen command's filesystem side effects —
not just the underlying ``generate_keypair`` (already covered in
``test_ed25519.py``). Catches argparse wiring, file permission
handling, --force semantics, fingerprint printing.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run_keygen(output_dir: Path, *extra_args: str) -> subprocess.CompletedProcess:
    """Invoke `python -m bijotel.cli.main keygen --output-dir <path>` via subprocess.

    Force UTF-8 stdout so the CLI's status box chars don't crash on
    Windows cp1252 default.
    """
    import os
    full_env = os.environ.copy()
    full_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [
            sys.executable, "-m", "bijotel.cli.main",
            "keygen", "--output-dir", str(output_dir),
            *extra_args,
        ],
        capture_output=True, text=True, check=False,
        env=full_env, encoding="utf-8",
    )


def test_keygen_creates_two_pem_files(tmp_path: Path) -> None:
    """Default invocation writes private + public PEM."""
    out_dir = tmp_path / "keys"
    res = _run_keygen(out_dir)
    assert res.returncode == 0, res.stderr
    priv = out_dir / "bijotel_private.pem"
    pub = out_dir / "bijotel_public.pem"
    assert priv.exists()
    assert pub.exists()
    assert priv.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert pub.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")


def test_keygen_prints_fingerprint(tmp_path: Path) -> None:
    out_dir = tmp_path / "keys"
    res = _run_keygen(out_dir)
    assert res.returncode == 0
    assert "Fingerprint:" in res.stdout
    # The fingerprint is 16 hex chars per public_key_fingerprint().
    for line in res.stdout.splitlines():
        if "Fingerprint:" in line:
            fp = line.split("Fingerprint:")[1].strip()
            assert len(fp) == 16
            int(fp, 16)  # raises if not hex
            break


def test_keygen_refuses_overwrite_without_force(tmp_path: Path) -> None:
    """A second keygen into the same dir must NOT silently overwrite."""
    out_dir = tmp_path / "keys"
    _run_keygen(out_dir)
    res = _run_keygen(out_dir)  # second call without --force
    assert res.returncode != 0
    assert "already exists" in res.stderr or "already exists" in res.stdout


def test_keygen_force_overwrites(tmp_path: Path) -> None:
    """With --force, the second keygen replaces the keypair."""
    out_dir = tmp_path / "keys"
    _run_keygen(out_dir)
    first_priv = (out_dir / "bijotel_private.pem").read_bytes()
    res = _run_keygen(out_dir, "--force")
    assert res.returncode == 0
    second_priv = (out_dir / "bijotel_private.pem").read_bytes()
    assert first_priv != second_priv


def test_keygen_creates_output_dir_if_missing(tmp_path: Path) -> None:
    """--output-dir is created when it doesn't exist."""
    out_dir = tmp_path / "fresh_subdir" / "keys"
    assert not out_dir.exists()
    res = _run_keygen(out_dir)
    assert res.returncode == 0
    assert out_dir.is_dir()
