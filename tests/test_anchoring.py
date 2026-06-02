"""Tests for Rekor anchoring (v2.9.0; ECDSA P-256 live-interop fix v2.13.2).

The Rekor HTTP client is mocked via monkeypatch so most tests are fast and
hermetic — no network needed. The ECDSA P-256 signing path is real (uses
``bijotel.crypto.ecdsa_p256``) and the chain.db setup is real (uses
``HmacChainSpanProcessor``), so the crypto/storage seam is covered.

History note: anchors were originally signed with Ed25519, but Rekor verifies
Ed25519 hashedrekord entries via Ed25519ph (pre-hashed EdDSA), which Python's
``cryptography`` cannot emit — so live submissions failed with "ed25519:
invalid signature". v2.13.2 switched anchoring to ECDSA P-256 (signs a SHA-256
digest, Rekor's canonical hashedrekord path). The mocked tests below assert the
new ECDSA/SHA-256 wire shape; ``test_live_rekor_roundtrip`` (env-gated) is the
non-mocked guard that actually hits rekor.sigstore.dev.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from bijotel.anchoring import (
    REKOR_PUBLIC_URL,
    AnchorVerifyResult,
    RekorAnchor,
    anchor_chain_head,
    verify_rekor_anchor,
)
from bijotel.crypto.ecdsa_p256 import generate_keypair, verify_digest

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _seed_chain_with_head(tmp_path: Path) -> tuple[Path, str, int]:
    """Create a chain.db with one real sealed row. Returns (db, head_hash, head_seq)."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    from bijotel.processors import HmacChainSpanProcessor

    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db, secret_key=b"x" * 32)
    )
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("test.anchor") as span:
        span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        span.set_attribute("gen_ai.usage.input_tokens", 100)
        span.set_attribute("gen_ai.usage.output_tokens", 50)
    provider.shutdown()

    with sqlite3.connect(db) as conn:
        seq, hmac_hash = conn.execute(
            "SELECT seq, hmac_hash FROM chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    return db, hmac_hash, int(seq)


def _ecdsa_keypair(tmp_path: Path) -> tuple[Path, Path, bytes, bytes]:
    """Generate a fresh ECDSA P-256 keypair and write it to PEM files."""
    priv_pem, pub_pem = generate_keypair()
    priv_path = tmp_path / "ecdsa_private.pem"
    pub_path = tmp_path / "ecdsa_public.pem"
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)
    return priv_path, pub_path, priv_pem, pub_pem


def _fake_rekor_response(
    *,
    log_index: int = 12345,
    log_uuid: str = "abc123def456",
    integrated_time: int = 1716730000,
    data_hash_hex: str,
    signature_b64: str,
    public_pem_b64: str,
) -> bytes:
    """Encode a Rekor-shaped response body. body is base64-encoded JSON."""
    body_json = {
        "apiVersion": "0.0.1",
        "kind": "hashedrekord",
        "spec": {
            "data": {"hash": {"algorithm": "sha256", "value": data_hash_hex}},
            "signature": {
                "content": signature_b64,
                "publicKey": {"content": public_pem_b64},
            },
        },
    }
    payload = {
        log_uuid: {
            "body": base64.b64encode(
                json.dumps(body_json).encode("utf-8")
            ).decode("ascii"),
            "integratedTime": integrated_time,
            "logID": "deadbeef" * 8,
            "logIndex": log_index,
        }
    }
    return json.dumps(payload).encode("utf-8")


# ----------------------------------------------------------------------
# 1. anchor_chain_head
# ----------------------------------------------------------------------


def test_anchor_chain_head_uploads_correct_payload(tmp_path: Path) -> None:
    db, head_hash, head_seq = _seed_chain_with_head(tmp_path)
    priv_path, _pub_path, _priv_pem, pub_pem = _ecdsa_keypair(tmp_path)

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data
        head_bytes = bytes.fromhex(head_hash)
        data_hash = hashlib.sha256(head_bytes).hexdigest()
        resp = _fake_rekor_response(
            data_hash_hex=data_hash,
            signature_b64="UExBQ0VIT0xERVI=",  # b64("PLACEHOLDER")
            public_pem_b64=base64.b64encode(pub_pem).decode("ascii"),
        )
        return io.BytesIO(resp)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        anchor = anchor_chain_head(db, sign_key_pem=priv_path)

    assert "/api/v1/log/entries" in captured["url"]
    sent = json.loads(captured["body"])
    assert sent["kind"] == "hashedrekord"
    # ECDSA P-256 entries carry a SHA-256 digest (NOT sha512 — that's the bug fix).
    assert sent["spec"]["data"]["hash"]["algorithm"] == "sha256"
    expected_hash = hashlib.sha256(bytes.fromhex(head_hash)).hexdigest()
    assert sent["spec"]["data"]["hash"]["value"] == expected_hash

    assert anchor.log_index == 12345
    assert anchor.log_uuid == "abc123def456"
    assert anchor.integrated_time == 1716730000
    assert anchor.head_hash == head_hash
    assert anchor.head_seq == head_seq
    assert anchor.rekor_url == REKOR_PUBLIC_URL


def test_anchor_chain_head_signature_is_real_ecdsa(tmp_path: Path) -> None:
    """The signature embedded in the anchor must verify with the public key."""
    db, head_hash, _seq = _seed_chain_with_head(tmp_path)
    priv_path, _pub_path, _priv_pem, pub_pem = _ecdsa_keypair(tmp_path)

    def fake_urlopen(req, timeout):
        head_bytes = bytes.fromhex(head_hash)
        data_hash = hashlib.sha256(head_bytes).hexdigest()
        sent = json.loads(req.data)
        return io.BytesIO(_fake_rekor_response(
            data_hash_hex=data_hash,
            signature_b64=sent["spec"]["signature"]["content"],
            public_pem_b64=sent["spec"]["signature"]["publicKey"]["content"],
        ))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        anchor = anchor_chain_head(db, sign_key_pem=priv_path)

    # The DER ECDSA signature verifies over SHA-256(head_bytes) — exactly what
    # Rekor checks. verify_digest re-hashes head_bytes with SHA-256 internally.
    sig_bytes = base64.b64decode(anchor.signature_b64)
    assert verify_digest(bytes.fromhex(head_hash), sig_bytes, pub_pem) is True


def test_anchor_chain_head_rejects_ed25519_key(tmp_path: Path) -> None:
    """An Ed25519 key must be rejected with a clear, actionable error.

    Rekor cannot verify Ed25519 hashedrekord entries from Python — this guard
    stops a confusing live 'invalid signature' failure at sign time instead.
    """
    from bijotel.crypto.ed25519 import generate_keypair as ed_generate

    db, _head_hash, _seq = _seed_chain_with_head(tmp_path)
    ed_priv, _ed_pub = ed_generate()
    ed_path = tmp_path / "ed25519_private.pem"
    ed_path.write_bytes(ed_priv)

    with pytest.raises(ValueError, match="ECDSA P-256"):
        anchor_chain_head(db, sign_key_pem=ed_path)


def test_anchor_chain_head_missing_db_raises(tmp_path: Path) -> None:
    priv_path, _pub, _, _ = _ecdsa_keypair(tmp_path)
    with pytest.raises(FileNotFoundError):
        anchor_chain_head(tmp_path / "nope.db", sign_key_pem=priv_path)


def test_anchor_chain_head_empty_chain_raises(tmp_path: Path) -> None:
    """An empty chain table → RuntimeError (not a silent zero-row anchor)."""
    db = tmp_path / "chain.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE chain (seq INTEGER PRIMARY KEY, hmac_hash TEXT)")
    priv_path, _pub, _, _ = _ecdsa_keypair(tmp_path)
    with pytest.raises(RuntimeError, match="empty"):
        anchor_chain_head(db, sign_key_pem=priv_path)


# ----------------------------------------------------------------------
# 2. verify_rekor_anchor — happy + each mismatch
# ----------------------------------------------------------------------


def _build_anchor_and_response(tmp_path: Path) -> tuple[RekorAnchor, bytes, bytes]:
    """Helper: create a real anchor + the matching Rekor response bytes."""
    db, head_hash, _ = _seed_chain_with_head(tmp_path)
    priv_path, _pub_path, _priv_pem, pub_pem = _ecdsa_keypair(tmp_path)

    head_bytes = bytes.fromhex(head_hash)
    data_hash = hashlib.sha256(head_bytes).hexdigest()
    pub_pem_b64 = base64.b64encode(pub_pem).decode("ascii")

    def fake_urlopen(req, timeout):
        sent = json.loads(req.data)
        return io.BytesIO(_fake_rekor_response(
            data_hash_hex=data_hash,
            signature_b64=sent["spec"]["signature"]["content"],
            public_pem_b64=sent["spec"]["signature"]["publicKey"]["content"],
        ))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        anchor = anchor_chain_head(db, sign_key_pem=priv_path)

    matching_response = _fake_rekor_response(
        data_hash_hex=data_hash,
        signature_b64=anchor.signature_b64,
        public_pem_b64=pub_pem_b64,
    )
    return anchor, matching_response, pub_pem


def test_verify_rekor_anchor_match(tmp_path: Path) -> None:
    anchor, matching_response, _pub_pem = _build_anchor_and_response(tmp_path)

    with patch("urllib.request.urlopen", return_value=io.BytesIO(matching_response)):
        result = verify_rekor_anchor(anchor)

    assert result.match is True
    assert result.hash_matches is True
    assert result.pubkey_matches is True
    assert result.signature_valid is True
    assert result.reason is None


def test_verify_rekor_anchor_with_external_pubkey(tmp_path: Path) -> None:
    """Passing expected_public_key_pem binds the check to an external trust anchor."""
    anchor, matching_response, pub_pem = _build_anchor_and_response(tmp_path)

    with patch("urllib.request.urlopen", return_value=io.BytesIO(matching_response)):
        result = verify_rekor_anchor(anchor, expected_public_key_pem=pub_pem)

    assert result.match is True


def test_verify_rekor_anchor_hash_mismatch(tmp_path: Path) -> None:
    """If Rekor's hash doesn't match what we recompute, verify fails."""
    anchor, _matching, pub_pem = _build_anchor_and_response(tmp_path)
    wrong_response = _fake_rekor_response(
        data_hash_hex="0" * 64,  # nonsense sha256 (64 hex chars)
        signature_b64=anchor.signature_b64,
        public_pem_b64=base64.b64encode(pub_pem).decode("ascii"),
    )

    with patch("urllib.request.urlopen", return_value=io.BytesIO(wrong_response)):
        result = verify_rekor_anchor(anchor)

    assert result.match is False
    assert result.hash_matches is False
    assert "hash mismatch" in (result.reason or "")


def test_verify_rekor_anchor_pubkey_mismatch(tmp_path: Path) -> None:
    """A different pubkey in Rekor (e.g. forged anchor) fails the external check."""
    anchor, _matching, pub_pem = _build_anchor_and_response(tmp_path)
    _other_priv, other_pub = generate_keypair()
    head_bytes = bytes.fromhex(anchor.head_hash)

    wrong_response = _fake_rekor_response(
        data_hash_hex=hashlib.sha256(head_bytes).hexdigest(),
        signature_b64=anchor.signature_b64,
        public_pem_b64=base64.b64encode(other_pub).decode("ascii"),
    )

    with patch("urllib.request.urlopen", return_value=io.BytesIO(wrong_response)):
        result = verify_rekor_anchor(
            anchor, expected_public_key_pem=pub_pem
        )

    assert result.match is False
    assert result.pubkey_matches is False
    assert "public key" in (result.reason or "")


def test_verify_rekor_anchor_signature_invalid(tmp_path: Path) -> None:
    """Tampering with the anchor's signature_b64 breaks verify."""
    anchor, matching_response, _pub_pem = _build_anchor_and_response(tmp_path)

    sig = anchor.signature_b64
    flipped = ("z" if sig[0] != "z" else "y") + sig[1:]
    bad_anchor = RekorAnchor(
        rekor_url=anchor.rekor_url,
        log_index=anchor.log_index,
        log_uuid=anchor.log_uuid,
        integrated_time=anchor.integrated_time,
        head_hash=anchor.head_hash,
        head_seq=anchor.head_seq,
        signature_b64=flipped,
        public_key_pem=anchor.public_key_pem,
    )

    with patch("urllib.request.urlopen", return_value=io.BytesIO(matching_response)):
        result = verify_rekor_anchor(bad_anchor)

    assert result.match is False
    assert result.signature_valid is False
    assert "signature" in (result.reason or "").lower()


def test_verify_rekor_anchor_404_returns_clean_result(tmp_path: Path) -> None:
    """If Rekor responds 404 (entry not in log), we return match=False with a reason."""
    anchor, _matching, _pub = _build_anchor_and_response(tmp_path)
    import urllib.error

    def raise_404(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error":"not found"}')
        )

    with patch("urllib.request.urlopen", side_effect=raise_404):
        result = verify_rekor_anchor(anchor)

    assert result.match is False
    assert "not found" in (result.reason or "").lower()


# ----------------------------------------------------------------------
# 3. Dataclasses
# ----------------------------------------------------------------------


def test_rekor_anchor_to_dict_json_roundtrip() -> None:
    anchor = RekorAnchor(
        rekor_url=REKOR_PUBLIC_URL,
        log_index=42,
        log_uuid="uuid-test",
        integrated_time=1716730000,
        head_hash="a" * 64,
        head_seq=10,
        signature_b64="c2lnbmF0dXJl",
        public_key_pem="-----BEGIN PUBLIC KEY-----\nFAKE\n-----END PUBLIC KEY-----",
    )
    d = anchor.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["log_index"] == 42


def test_anchor_verify_result_to_dict() -> None:
    r = AnchorVerifyResult(
        match=True,
        log_index=1,
        integrated_time=100,
        hash_matches=True,
        pubkey_matches=True,
        signature_valid=True,
    )
    d = r.to_dict()
    assert d["match"] is True
    assert json.loads(json.dumps(d)) == d


# ----------------------------------------------------------------------
# 4. CLI (subprocess) — happy publish + verify round-trip
# ----------------------------------------------------------------------


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


def test_cli_anchor_publish_writes_sidecar(tmp_path: Path) -> None:
    """`bijotel anchor publish` runs end-to-end against a local fake Rekor."""
    import http.server
    import socketserver
    import threading

    db, head_hash, _seq = _seed_chain_with_head(tmp_path)
    priv_path, _pub_path, _priv_pem, pub_pem = _ecdsa_keypair(tmp_path)

    captured_requests = []

    class FakeRekor(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            captured_requests.append(body)

            sent = json.loads(body)
            head_bytes = bytes.fromhex(head_hash)
            data_hash = hashlib.sha256(head_bytes).hexdigest()
            response = _fake_rekor_response(
                data_hash_hex=data_hash,
                signature_b64=sent["spec"]["signature"]["content"],
                public_pem_b64=sent["spec"]["signature"]["publicKey"]["content"],
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), FakeRekor) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            sidecar = tmp_path / "anchor.json"
            out = _run_cli(
                "anchor", "publish",
                "--db", str(db),
                "--sign-key", str(priv_path),
                "--output", str(sidecar),
                "--rekor-url", f"http://127.0.0.1:{port}",
            )
        finally:
            httpd.shutdown()

    assert out.returncode == 0, out.stderr
    assert "PUBLISHED" in out.stdout
    assert "log_index" in out.stdout
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["head_hash"] == head_hash


def test_cli_anchor_publish_missing_db_exits_1(tmp_path: Path) -> None:
    priv_path, _pub, _, _ = _ecdsa_keypair(tmp_path)
    out = _run_cli(
        "anchor", "publish",
        "--db", str(tmp_path / "no-such.db"),
        "--sign-key", str(priv_path),
    )
    assert out.returncode == 1
    assert "not found" in out.stderr.lower()


# ----------------------------------------------------------------------
# 5. Public API exports
# ----------------------------------------------------------------------


def test_public_api_exports() -> None:
    import bijotel

    for name in (
        "RekorAnchor",
        "AnchorVerifyResult",
        "anchor_chain_head",
        "verify_rekor_anchor",
        "REKOR_PUBLIC_URL",
    ):
        assert hasattr(bijotel, name), f"bijotel.{name} missing"
        assert name in bijotel.__all__


# ----------------------------------------------------------------------
# 6. LIVE Rekor round-trip (non-mocked) — the guard that would have caught
#    the Ed25519ph bug. Opt-in: set BIJOTEL_REKOR_LIVE=1 (hits the network).
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("BIJOTEL_REKOR_LIVE") != "1",
    reason="set BIJOTEL_REKOR_LIVE=1 to hit the real rekor.sigstore.dev",
)
def test_live_rekor_roundtrip(tmp_path: Path) -> None:
    """Publish a real anchor to public Rekor and fetch it back. No mocks."""
    db, _head_hash, _seq = _seed_chain_with_head(tmp_path)
    priv_path, _pub_path, _priv_pem, pub_pem = _ecdsa_keypair(tmp_path)

    anchor = anchor_chain_head(db, sign_key_pem=priv_path)
    assert anchor.log_index > 0, "Rekor should return a positive log index"

    result = verify_rekor_anchor(anchor, expected_public_key_pem=pub_pem)
    assert result.match is True, f"live verify failed: {result.reason}"
    assert result.hash_matches and result.pubkey_matches and result.signature_valid
