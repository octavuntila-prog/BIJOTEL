"""End-to-end tests for v2 signed exports (v2.1.0).

Covers:
- v2 export schema and signature block contents.
- Backward compatibility: unsigned export remains v1; v1 verification path
  unchanged.
- Three verify modes: HMAC only, HMAC + pubkey, pubkey only (auditor mode).
- Tampering detection on the signature block AND on the embedded public key
  (key-swap attack).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.crypto.ed25519 import generate_keypair
from bijotel.processors import (
    HmacChainSpanProcessor,
    export_chain,
    inspect_export,
    verify_export,
)

SECRET = b"x" * 32


@pytest.fixture
def chain_db(tmp_path: Path) -> Path:
    """Build a chain.db with 3 spans by emitting via real TracerProvider."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test")
    for i in range(3):
        with tracer.start_as_current_span(f"test-span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 10 + i)
            span.set_attribute("gen_ai.usage.output_tokens", 5)

    provider.shutdown()
    return db


@pytest.fixture
def keypair_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Persist an Ed25519 keypair on disk; return (private_pem, public_pem)."""
    priv, pub = generate_keypair()
    priv_path = tmp_path / "private.pem"
    pub_path = tmp_path / "public.pem"
    priv_path.write_bytes(priv)
    pub_path.write_bytes(pub)
    return priv_path, pub_path


# ──────────────────── format / backward-compat ────────────────────


def test_export_without_sign_key_produces_v1(chain_db: Path, tmp_path: Path) -> None:
    """No --sign-key → format stays bijotel-chain-v1 (backward compat)."""
    out = tmp_path / "v1.json"
    export_chain(chain_db, out, SECRET)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["format"] == "bijotel-chain-v1"
    assert "ed25519_signature" not in data


def test_export_with_sign_key_produces_v2(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """--sign-key → format flips to bijotel-chain-v2 with signature block."""
    priv_path, _ = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["format"] == "bijotel-chain-v2"
    assert "ed25519_signature" in data


def test_v2_signature_block_has_required_fields(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    priv_path, _ = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    block = json.loads(out.read_text(encoding="utf-8"))["ed25519_signature"]
    assert block["algorithm"] == "Ed25519"
    assert block["signed_over"] == "chain_signature"
    assert len(base64.b64decode(block["public_key"])) == 32
    assert len(base64.b64decode(block["signature"])) == 64


# ──────────────────── verify paths ────────────────────


def test_verify_v1_with_secret_passes(chain_db: Path, tmp_path: Path) -> None:
    """v1 verify path unchanged from earlier versions."""
    out = tmp_path / "v1.json"
    export_chain(chain_db, out, SECRET)
    valid, reason = verify_export(out, SECRET)
    assert valid is True, reason


def test_verify_v2_with_secret_only_passes(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """v2 export verifies under HMAC secret even without --public-key."""
    priv_path, _ = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    valid, reason = verify_export(out, SECRET)
    assert valid is True, reason


def test_verify_v2_with_secret_and_pubkey_passes(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """Both layers verified together."""
    priv_path, pub_path = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    valid, reason = verify_export(out, SECRET, public_key_path=pub_path)
    assert valid is True, reason


def test_verify_v2_auditor_mode_no_secret(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """Auditor mode: pubkey only, no HMAC secret. Should pass on a v2 export."""
    priv_path, pub_path = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    valid, reason = verify_export(out, secret_key=None, public_key_path=pub_path)
    assert valid is True, reason


def test_verify_auditor_mode_against_v1_export_rejected(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """v1 file with pubkey-only verify is refused — no signature to check."""
    _priv_path, pub_path = keypair_paths
    out = tmp_path / "v1.json"
    export_chain(chain_db, out, SECRET)  # v1, no sign_key
    valid, reason = verify_export(out, secret_key=None, public_key_path=pub_path)
    assert valid is False
    assert reason is not None and "v2" in reason


def test_verify_v2_wrong_pubkey_fails(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """Auditor's public key differs from the embedded one → rejected."""
    priv_path, _real_pub_path = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    # Generate a *different* keypair and feed only that public key as
    # the auditor's "trusted" copy.
    _other_priv, other_pub = generate_keypair()
    other_pub_path = tmp_path / "imposter_public.pem"
    other_pub_path.write_bytes(other_pub)
    valid, reason = verify_export(
        out, secret_key=None, public_key_path=other_pub_path
    )
    assert valid is False
    assert reason is not None and "public key" in reason.lower()


def test_verify_v2_tampered_signature_fails(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """Flip a byte in the signature → fails."""
    priv_path, pub_path = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    sig_bytes = bytearray(base64.b64decode(data["ed25519_signature"]["signature"]))
    sig_bytes[0] ^= 0xFF
    data["ed25519_signature"]["signature"] = base64.b64encode(sig_bytes).decode("ascii")
    out.write_text(json.dumps(data), encoding="utf-8")
    valid, reason = verify_export(out, secret_key=None, public_key_path=pub_path)
    assert valid is False
    assert reason is not None and "ed25519" in reason.lower()


def test_verify_v2_swapped_embedded_pubkey_fails(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """Attacker swaps embedded pubkey to their own → mismatch with auditor's copy."""
    priv_path, pub_path = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    # Replace embedded pubkey with the raw bytes of an attacker key.
    _, attacker_pub = generate_keypair()
    from bijotel.crypto.ed25519 import public_key_raw_b64
    data["ed25519_signature"]["public_key"] = public_key_raw_b64(attacker_pub)
    out.write_text(json.dumps(data), encoding="utf-8")
    valid, reason = verify_export(out, secret_key=None, public_key_path=pub_path)
    assert valid is False
    assert reason is not None and (
        "public key" in reason.lower() or "key-swap" in reason.lower()
    )


def test_verify_v2_tampered_entry_body_still_fails_under_pubkey_only(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """Auditor mode still catches canonical_body tamper via body-hash check."""
    priv_path, pub_path = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    # Corrupt one byte of the first entry's canonical_body.
    body_b64 = data["entries"][0]["canonical_body_b64"]
    body_bytes = bytearray(base64.b64decode(body_b64))
    body_bytes[0] ^= 0x01
    data["entries"][0]["canonical_body_b64"] = base64.b64encode(body_bytes).decode("ascii")
    out.write_text(json.dumps(data), encoding="utf-8")
    valid, reason = verify_export(out, secret_key=None, public_key_path=pub_path)
    assert valid is False
    assert reason is not None and "canonical_body" in reason


def test_verify_v2_pubkey_only_skips_per_entry_hmac_check(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """Auditor mode does NOT re-check per-entry HMACs (needs secret).

    Proof: tamper a hmac_hash field. Without the secret the verifier
    can't tell — but it still passes because the Ed25519 signature
    over chain_signature still holds. (The auditor trusts the
    operator's attestation that the chain_signature reflects a
    coherent HMAC chain.)
    """
    priv_path, pub_path = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    data = json.loads(out.read_text(encoding="utf-8"))
    # Tamper hmac_hash of seq=2 only (don't change anything in the chain
    # link structure: prev_hash[N+1] still uses the original value of
    # hmac_hash[N], so chain-link consistency is broken). Keep chain
    # links intact AND tamper only the orphan-looking hmac value to
    # be sure auditor doesn't notice.
    last = data["entries"][-1]
    # Last entry has no successor referencing its hmac, so we can change
    # it without breaking chain-link verification. Operator-side HMAC
    # check WOULD catch this. Auditor-mode (no secret) won't.
    last["hmac_hash"] = "ff" * 32
    # head_hash mirrors the last hmac_hash — keep them in sync so the
    # head_hash sanity check stays clean. The Ed25519 signature was
    # signed over the ORIGINAL chain_signature value (which we don't
    # touch here), so the outer attestation still verifies.
    data["head_hash"] = "ff" * 32
    out.write_text(json.dumps(data), encoding="utf-8")
    valid, reason = verify_export(out, secret_key=None, public_key_path=pub_path)
    # Note: the head_hash != last.hmac_hash invariant is still satisfied
    # (we changed both together), so auditor mode lets this through.
    # That's the documented limitation — fixed by also signing entries.
    # Future enhancement: bind Ed25519 sig over entries digest too.
    assert valid is True, reason
    # ...but if we ALSO ran with the secret, the operator-side checks
    # would catch it. The first failure encountered is either the
    # chain_signature mismatch (HMAC over <head_hash>:<entries_count>
    # changed when we updated head_hash) or the per-entry hmac_hash
    # mismatch — both are real catches, so accept either reason.
    valid2, reason2 = verify_export(out, SECRET, public_key_path=pub_path)
    assert valid2 is False
    assert reason2 is not None
    assert ("hmac_hash" in reason2) or ("chain_signature" in reason2), reason2


# ──────────────────── inspect_export ────────────────────


def test_inspect_export_v1_reports_unsigned(chain_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "v1.json"
    export_chain(chain_db, out, SECRET)
    info = inspect_export(out)
    assert info["format"] == "bijotel-chain-v1"
    assert info["signed"] is False
    assert info["entries_count"] == 3
    assert info["size_bytes"] > 0


def test_inspect_export_v2_reports_fingerprint(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    priv_path, _ = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    info = inspect_export(out)
    assert info["format"] == "bijotel-chain-v2"
    assert info["signed"] is True
    fp = info["public_key_fingerprint"]
    assert isinstance(fp, str)
    assert len(fp) == 16


def test_inspect_export_missing_file(tmp_path: Path) -> None:
    info = inspect_export(tmp_path / "does_not_exist.json")
    assert "error" in info


def test_inspect_export_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    info = inspect_export(bad)
    assert "error" in info


# ──────────────────── misc edge cases ────────────────────


def test_export_with_nonexistent_sign_key_raises(
    chain_db: Path, tmp_path: Path
) -> None:
    """Bad --sign-key path surfaces a clean FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        export_chain(
            chain_db,
            tmp_path / "out.json",
            SECRET,
            sign_key_path=tmp_path / "missing.pem",
        )


def test_verify_export_no_secret_no_pubkey_is_rejected(
    chain_db: Path, tmp_path: Path, keypair_paths: tuple[Path, Path]
) -> None:
    """ISSUE-8 fix (audit 2026-06-08): the library-level verify_export now
    REJECTS a call with neither secret nor public_key. With no trust anchor
    only self-referential structure is checked, which a forged export can
    satisfy (it previously returned (True, None) — a false VALID). The
    library contract is now honest, matching the CLI gate.
    """
    priv_path, _ = keypair_paths
    out = tmp_path / "v2.json"
    export_chain(chain_db, out, SECRET, sign_key_path=priv_path)
    valid, reason = verify_export(out, secret_key=None, public_key_path=None)
    assert valid is False
    assert "trust anchor" in (reason or "")
