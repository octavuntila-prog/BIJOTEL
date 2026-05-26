"""Software-key attestation backend.

Not hardware-rooted. Honest about it: the ``backend`` field in every
quote is the literal string ``"software-key"``, the CHANGELOG flags
this as software-only, and the docs section 5 spells out what it
*does* and *does not* prove.

What it proves:

  * The BIJOTEL package files on disk hashed to a specific SHA-256
    when ``attest()`` was called (catches between-install-and-run
    tampering of the .py files).
  * The platform was as described (OS, arch, Python version,
    hostname, bijotel version).
  * A specific Ed25519 key signed the canonical payload (operator
    identity).

What it does NOT prove (these need real TEE backends):

  * The CPU wasn't compromised
  * Memory wasn't being read by a malicious hypervisor
  * The Ed25519 key was generated in a secure enclave

Same honest discipline as substrate-guard L5: ship the software
signature path now with explicit ``tpm_available: false``-equivalent
labeling; upgrade to hardware-rooted backends when the host supports
them. Same interface, same archive schema, same verify story.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import platform
import socket
from dataclasses import replace
from pathlib import Path

from bijotel.attestation.base import AttestationQuote
from bijotel.crypto.ed25519 import sign as ed25519_sign
from bijotel.crypto.ed25519 import verify as ed25519_verify


class SoftwareAttestation:
    """Ed25519 + code-measurement + platform-info attestation.

    Pass an Ed25519 private key PEM at construction. ``attest(data)``
    produces a quote; ``verify(quote, data)`` checks it against the
    matching public key.

    For audit-grade verification the verifier should supply the
    public key out-of-band — same pattern as Rekor anchor verify with
    ``--public-key``.

    Args:
        private_key_pem: PEM bytes of an Ed25519 private key (from
            ``bijotel keygen`` or any standard generator).
        public_key_pem: Optional. PEM bytes of the matching public
            key. If omitted, ``verify`` derives it from the private
            key (fine for round-trip self-check; for external verify
            pass it explicitly).
    """

    #: Required by the AttestationBackend protocol.
    name = "software-key"

    def __init__(
        self,
        private_key_pem: bytes,
        *,
        public_key_pem: bytes | None = None,
    ) -> None:
        self._private_pem = private_key_pem
        self._public_pem = public_key_pem or _derive_public_pem(private_key_pem)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attest(self, data: bytes) -> AttestationQuote:
        """Produce a software-key attestation over ``data``.

        Steps:
          1. Measure code (SHA-256 of BIJOTEL package .py files).
          2. Collect platform info.
          3. Build canonical-JSON payload binding everything together.
          4. Ed25519-sign the payload bytes.
          5. Self-verify (sanity).
          6. Return ``AttestationQuote``.
        """
        timestamp = (
            datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        )
        data_hash_hex = hashlib.sha256(data).hexdigest()
        code_measurement = _measure_code()
        platform_info = _collect_platform_info()

        payload = _build_payload(
            data_hash=data_hash_hex,
            code_measurement=code_measurement,
            platform_info=platform_info,
            timestamp=timestamp,
        )
        signature_bytes = ed25519_sign(payload, self._private_pem)
        quote_b64 = base64.b64encode(signature_bytes).decode("ascii")

        quote = AttestationQuote(
            backend=self.name,
            quote_b64=quote_b64,
            code_measurement=code_measurement,
            platform_info=platform_info,
            timestamp=timestamp,
            data_hash=data_hash_hex,
            verified=False,  # filled in by self-check below
        )
        ok = self.verify(quote, data)
        return replace(quote, verified=ok)

    def verify(self, quote: AttestationQuote, data: bytes) -> bool:
        """Verify a quote against the matching public key.

        Returns ``True`` iff:
          * ``quote.backend == "software-key"``
          * ``quote.data_hash == sha256(data).hex()``
          * The Ed25519 signature in ``quote.quote_b64`` verifies
            against the payload reconstructed from quote fields.

        Cross-backend verifies (e.g. passing a tpm2 quote here) return
        ``False`` rather than raise — same behaviour as the other
        layer-by-layer verifiers in BIJOTEL.
        """
        if quote.backend != self.name:
            return False

        expected_data_hash = hashlib.sha256(data).hexdigest()
        if quote.data_hash != expected_data_hash:
            return False

        try:
            sig = base64.b64decode(quote.quote_b64)
        except (ValueError, base64.binascii.Error):
            return False

        payload = _build_payload(
            data_hash=quote.data_hash,
            code_measurement=quote.code_measurement,
            platform_info=quote.platform_info,
            timestamp=quote.timestamp,
        )
        try:
            return bool(ed25519_verify(payload, sig, self._public_pem))
        except Exception:
            return False


# ---------------------------------------------------------------------
# Helpers (module-private)
# ---------------------------------------------------------------------


def _build_payload(
    *,
    data_hash: str,
    code_measurement: str,
    platform_info: dict[str, str],
    timestamp: str,
) -> bytes:
    """Canonical-JSON payload that both attest + verify hash over.

    Sorted keys + compact separators = deterministic byte stream
    across Python versions. Don't change this without bumping the
    backend name (``software-key`` → ``software-key/v2``) — old
    archives would stop verifying otherwise.
    """
    payload_dict = {
        "data_hash": data_hash,
        "code_measurement": code_measurement,
        "platform": platform_info,
        "timestamp": timestamp,
    }
    return json.dumps(
        payload_dict, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _measure_code() -> str:
    """SHA-256 of all ``.py`` files in the bijotel package, sorted.

    Sort by relative path so the order is stable across filesystems.
    Skip ``__pycache__/`` because compiled bytecode varies across
    Python versions and would make the hash non-deterministic on
    upgrade.

    Returns the hex digest, 64 chars.
    """
    import bijotel  # local import — avoid circular at module load

    pkg_dir = Path(bijotel.__file__).parent
    hasher = hashlib.sha256()
    py_files = sorted(
        f for f in pkg_dir.rglob("*.py") if "__pycache__" not in f.parts
    )
    for f in py_files:
        # Path component for stability + the bytes themselves.
        rel = f.relative_to(pkg_dir).as_posix().encode("utf-8")
        hasher.update(rel)
        hasher.update(b"\x00")  # NUL separator so concat is unambiguous
        hasher.update(f.read_bytes())
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _collect_platform_info() -> dict[str, str]:
    """Five fixed keys describing the runtime environment."""
    import bijotel  # local import — avoids circular at module-load

    return {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "python": platform.python_version(),
        "hostname": socket.gethostname(),
        "bijotel_version": getattr(bijotel, "__version__", "unknown"),
    }


def _derive_public_pem(private_pem: bytes) -> bytes:
    """Recover the public PEM from the private PEM.

    Wrap cryptography's primitive so the rest of the module stays
    cryptography-free at import time. Used when the caller doesn't
    pass an explicit ``public_key_pem``.
    """
    from cryptography.hazmat.primitives import serialization

    priv = serialization.load_pem_private_key(private_pem, password=None)
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
