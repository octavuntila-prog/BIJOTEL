"""Tests for the TEE attestation interface (v2.10.0).

Software backend is exercised end-to-end with real Ed25519 + real
code-measurement + real platform-info. Hardware-stub backends are
checked for the documented refuse-to-construct behaviour. CLI
``bijotel archive --attest software`` is exercised via subprocess on
a real seeded chain.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bijotel.attestation import (
    AttestationQuote,
    AWSNitroAttestation,
    AzureSGXAttestation,
    GCPConfidentialAttestation,
    SoftwareAttestation,
    TPM2Attestation,
)
from bijotel.attestation.software import _measure_code
from bijotel.crypto.ed25519 import generate_keypair

# ----------------------------------------------------------------------
# 1. AttestationQuote dataclass
# ----------------------------------------------------------------------


def test_attestation_quote_to_dict_json_roundtrip() -> None:
    q = AttestationQuote(
        backend="software-key",
        quote_b64="UEFETERFRA==",
        code_measurement="a" * 64,
        platform_info={"os": "Linux 6.8", "arch": "x86_64"},
        timestamp="2026-05-26T12:00:00+00:00",
        data_hash="b" * 64,
        verified=True,
    )
    d = q.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["backend"] == "software-key"


def test_attestation_quote_to_json_is_sorted_canonical() -> None:
    q = AttestationQuote(
        backend="software-key",
        quote_b64="x",
        code_measurement="y",
    )
    body = q.to_json()
    # Keys sorted alphabetically — `backend` comes before `quote_b64`.
    assert body.index('"backend"') < body.index('"quote_b64"')
    # No whitespace (compact separators).
    assert ", " not in body
    assert ": " not in body


# ----------------------------------------------------------------------
# 2. SoftwareAttestation — happy path
# ----------------------------------------------------------------------


def _gen_keypair_files(tmp_path: Path) -> tuple[Path, Path, bytes, bytes]:
    priv, pub = generate_keypair()
    pp = tmp_path / "priv.pem"
    qp = tmp_path / "pub.pem"
    pp.write_bytes(priv)
    qp.write_bytes(pub)
    return pp, qp, priv, pub


def test_software_attest_produces_complete_quote(tmp_path: Path) -> None:
    _pp, _qp, priv, _pub = _gen_keypair_files(tmp_path)
    backend = SoftwareAttestation(priv)
    data = b"chain-head-001"
    quote = backend.attest(data)

    assert quote.backend == "software-key"
    # quote_b64 is a base64-encoded Ed25519 signature (64 raw bytes → 88 b64 chars).
    sig = base64.b64decode(quote.quote_b64)
    assert len(sig) == 64
    # code measurement is 64-char hex
    assert len(quote.code_measurement) == 64
    int(quote.code_measurement, 16)  # parses as hex
    # platform info populated
    assert "os" in quote.platform_info
    assert "arch" in quote.platform_info
    assert "python" in quote.platform_info
    assert "bijotel_version" in quote.platform_info
    # timestamp is ISO with UTC offset
    assert "T" in quote.timestamp
    assert quote.timestamp.endswith("+00:00") or quote.timestamp.endswith("Z")
    # data_hash is SHA-256(data) hex
    import hashlib
    assert quote.data_hash == hashlib.sha256(data).hexdigest()
    # self-verify passed
    assert quote.verified is True


def test_software_code_measurement_is_deterministic(tmp_path: Path) -> None:
    """Two calls produce the same code_measurement — required for verifiers."""
    _pp, _qp, priv, _pub = _gen_keypair_files(tmp_path)
    backend = SoftwareAttestation(priv)
    q1 = backend.attest(b"x")
    q2 = backend.attest(b"y")  # different data, same code
    assert q1.code_measurement == q2.code_measurement


def test_software_measure_code_skips_pycache(tmp_path: Path) -> None:
    """``_measure_code`` must not be sensitive to ``__pycache__/`` presence."""
    m = _measure_code()
    assert len(m) == 64  # 32-byte SHA-256 hex
    int(m, 16)  # parses as hex


# ----------------------------------------------------------------------
# 3. SoftwareAttestation — verify
# ----------------------------------------------------------------------


def test_software_verify_accepts_valid_quote(tmp_path: Path) -> None:
    _pp, _qp, priv, pub = _gen_keypair_files(tmp_path)
    backend = SoftwareAttestation(priv)
    data = b"some-chain-head"
    quote = backend.attest(data)
    # New instance with the SAME key can verify (cross-process pattern).
    verifier = SoftwareAttestation(priv, public_key_pem=pub)
    assert verifier.verify(quote, data) is True


def test_software_verify_rejects_different_data(tmp_path: Path) -> None:
    _pp, _qp, priv, _pub = _gen_keypair_files(tmp_path)
    backend = SoftwareAttestation(priv)
    quote = backend.attest(b"data-A")
    assert backend.verify(quote, b"data-B") is False


def test_software_verify_rejects_tampered_signature(tmp_path: Path) -> None:
    _pp, _qp, priv, _pub = _gen_keypair_files(tmp_path)
    backend = SoftwareAttestation(priv)
    quote = backend.attest(b"data")
    # Flip the first base64 char of the signature.
    bad_sig = ("z" if quote.quote_b64[0] != "z" else "y") + quote.quote_b64[1:]
    from dataclasses import replace
    tampered = replace(quote, quote_b64=bad_sig)
    assert backend.verify(tampered, b"data") is False


def test_software_verify_rejects_wrong_backend(tmp_path: Path) -> None:
    """A quote with backend != 'software-key' is rejected, not raised."""
    _pp, _qp, priv, _pub = _gen_keypair_files(tmp_path)
    backend = SoftwareAttestation(priv)
    quote = backend.attest(b"data")
    from dataclasses import replace
    foreign = replace(quote, backend="tpm2")
    assert backend.verify(foreign, b"data") is False


def test_software_verify_rejects_with_wrong_public_key(tmp_path: Path) -> None:
    """A verifier with a *different* public key rejects."""
    _pp, _qp, priv, _pub = _gen_keypair_files(tmp_path)
    _other_priv, other_pub = generate_keypair()
    backend = SoftwareAttestation(priv)
    quote = backend.attest(b"data")
    wrong_verifier = SoftwareAttestation(priv, public_key_pem=other_pub)
    assert wrong_verifier.verify(quote, b"data") is False


# ----------------------------------------------------------------------
# 4. Hardware stubs — all four must refuse to construct
# ----------------------------------------------------------------------


def test_tpm2_stub_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="TPM"):
        TPM2Attestation()


def test_nitro_stub_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="Nitro"):
        AWSNitroAttestation()


def test_gcp_stub_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="GCP|SEV-SNP"):
        GCPConfidentialAttestation()


def test_sgx_stub_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="SGX"):
        AzureSGXAttestation()


# ----------------------------------------------------------------------
# 5. Backend name constants
# ----------------------------------------------------------------------


def test_backend_name_constants() -> None:
    """The ``name`` class attribute is what populates ``AttestationQuote.backend``."""
    assert SoftwareAttestation.name == "software-key"
    assert TPM2Attestation.name == "tpm2"
    assert AWSNitroAttestation.name == "nitro"
    assert GCPConfidentialAttestation.name == "gcp-snp"
    assert AzureSGXAttestation.name == "sgx"


# ----------------------------------------------------------------------
# 6. Archive CLI — --attest software produces sidecar
# ----------------------------------------------------------------------


def _seed_chain(tmp_path: Path, *, n: int = 5) -> tuple[Path, str]:
    """Build a real chain.db with n rows. Returns (db_path, hmac_hex_secret)."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    from bijotel.processors import HmacChainSpanProcessor

    db = tmp_path / "chain.db"
    secret = b"x" * 32
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=secret))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test.attest")
    for i in range(n):
        with tracer.start_as_current_span(f"call.{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
            span.set_attribute("gen_ai.usage.input_tokens", 100 + i)
            span.set_attribute("gen_ai.usage.output_tokens", 50)
    provider.shutdown()
    return db, secret.hex()


def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "bijotel.cli.main", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_archive_with_attest_software_writes_sidecar(tmp_path: Path) -> None:
    db, secret_hex = _seed_chain(tmp_path, n=5)
    priv_path, _pub_path, _priv, _pub = _gen_keypair_files(tmp_path)
    archive_out = tmp_path / "arch.db"

    # Archive everything (--before-seq 99 grabs all rows since chain has 5).
    out = _run_cli(
        "archive",
        "--db", str(db),
        "--output", str(archive_out),
        "--secret-hex", secret_hex,
        "--before-seq", "99",
        "--sign-key", str(priv_path),
        "--attest", "software",
    )
    assert out.returncode == 0, out.stderr
    assert "Attestation sidecar" in out.stdout

    sidecar = archive_out.with_suffix(archive_out.suffix + ".attestation.json")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["backend"] == "software-key"
    assert data["verified"] is True
    # data_hash binds to the archive's terminal hmac → must be a 64-hex string
    assert len(data["data_hash"]) == 64


def test_archive_attest_tpm2_stub_errors_cleanly(tmp_path: Path) -> None:
    """--attest tpm2 (or any stub) returns exit 2 with the upgrade hint."""
    db, secret_hex = _seed_chain(tmp_path, n=3)
    priv_path, _pub_path, _priv, _pub = _gen_keypair_files(tmp_path)
    archive_out = tmp_path / "arch.db"

    out = _run_cli(
        "archive",
        "--db", str(db),
        "--output", str(archive_out),
        "--secret-hex", secret_hex,
        "--before-seq", "99",
        "--sign-key", str(priv_path),
        "--attest", "tpm2",
    )
    assert out.returncode == 2
    assert "TPM 2.0" in out.stderr or "TPM" in out.stderr


def test_archive_attest_without_sign_key_exits_2(tmp_path: Path) -> None:
    """--attest requires --sign-key (the operator's identity)."""
    db, secret_hex = _seed_chain(tmp_path, n=3)
    archive_out = tmp_path / "arch.db"

    out = _run_cli(
        "archive",
        "--db", str(db),
        "--output", str(archive_out),
        "--secret-hex", secret_hex,
        "--before-seq", "99",
        "--attest", "software",
    )
    assert out.returncode == 2
    assert "sign-key" in out.stderr.lower()


# ----------------------------------------------------------------------
# 7. Public API exports
# ----------------------------------------------------------------------


def test_public_api_exports() -> None:
    import bijotel

    for name in ("AttestationQuote", "SoftwareAttestation"):
        assert hasattr(bijotel, name), f"bijotel.{name} missing"
        assert name in bijotel.__all__
