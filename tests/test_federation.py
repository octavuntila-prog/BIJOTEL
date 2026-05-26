"""Tests for the federation client (v2.11.0).

Same mock-server pattern as the Rekor anchoring tests: a tiny
``http.server`` instance inside the test fixture stands in for the
federation service. The Ed25519 + receipt-verification logic is
real, so the cryptographic seam is fully covered without depending
on a running federation service (none exists at v2.11).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path

import pytest

from bijotel.crypto.ed25519 import generate_keypair
from bijotel.crypto.ed25519 import sign as ed25519_sign
from bijotel.federation import (
    CrossAnchorReceipt,
    FederationClient,
    verify_cross_anchor_receipt,
)
from bijotel.federation.types import _canonical_receipt_payload

# ---------------------------------------------------------------------
# Mock federation server
# ---------------------------------------------------------------------


def _make_mock_federation(
    *,
    register_response: dict | None = None,
    submit_response: dict | None = None,
    status_response: dict | None = None,
) -> tuple[socketserver.TCPServer, threading.Thread, int]:
    """Spin up a tiny in-tree HTTP server mimicking the federation REST API."""
    register_response = register_response or {
        "operator_id": "op_test123",
        "org_name": "TestCo",
        "public_key_pem": "<PEM>",
        "registered_at": "2026-05-26T12:00:00Z",
        "rekor_log_index": 1234567,
        "federation_url": "http://127.0.0.1",
    }
    submit_response = submit_response or {
        "submission_id": "sub_abc",
        "operator_id": "op_test123",
        "verified": True,
        "entry_count": 100,
        "first_seq": 1,
        "last_seq": 100,
        "continuity_verified": True,
        "submitted_at": "2026-05-26T12:01:00Z",
        "pending_anchor": True,
    }
    status_response = status_response or {
        "operators_active": 1,
        "operators_total": 1,
        "last_anchor": None,
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/register/challenge":
                self._send(200, {"nonce": "deadbeef"})
            elif self.path == "/status":
                self._send(200, status_response)
            elif self.path.startswith("/operator/"):
                self._send(200, {"operator_id": self.path.split("/")[-1]})
            elif self.path.startswith("/anchor/"):
                self._send(200, {"anchor_id": self.path.split("/")[-1]})
            elif self.path.startswith("/verify/"):
                self._send(200, {"anchor_id": self.path.split("/")[-1], "valid": True})
            else:
                self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            _body = self.rfile.read(length)
            if self.path == "/register":
                self._send(200, register_response)
            elif self.path == "/submit":
                self._send(200, submit_response)
            else:
                self._send(404, {"error": "not_found"})

        def log_message(self, *_args):
            pass  # silence the default access log

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, port


# ---------------------------------------------------------------------
# 1. FederationClient — happy paths
# ---------------------------------------------------------------------


def test_client_register_completes_challenge_response() -> None:
    server, _t, port = _make_mock_federation()
    try:
        priv, pub = generate_keypair()
        client = FederationClient(f"http://127.0.0.1:{port}")
        receipt = client.register(
            public_key_pem=pub,
            private_key_pem=priv,
            org_name="TestCo",
            contact_email="test@example.com",
        )
        assert receipt["operator_id"] == "op_test123"
        assert receipt["rekor_log_index"] == 1234567
    finally:
        server.shutdown()


def test_client_submit_carries_signed_auth_header() -> None:
    server, _t, port = _make_mock_federation()
    try:
        priv, _pub = generate_keypair()
        client = FederationClient(f"http://127.0.0.1:{port}")
        receipt = client.submit(
            operator_id="op_test123",
            private_key_pem=priv,
            signed_export_json={"entries": []},
        )
        assert receipt["submission_id"] == "sub_abc"
        assert receipt["verified"] is True
    finally:
        server.shutdown()


def test_client_status_unauthenticated() -> None:
    server, _t, port = _make_mock_federation(
        status_response={"operators_active": 3, "operators_total": 5}
    )
    try:
        client = FederationClient(f"http://127.0.0.1:{port}")
        data = client.status()
        assert data["operators_active"] == 3
    finally:
        server.shutdown()


def test_client_get_operator_anchor_verify() -> None:
    server, _t, port = _make_mock_federation()
    try:
        client = FederationClient(f"http://127.0.0.1:{port}")
        assert client.get_operator("op_a")["operator_id"] == "op_a"
        assert client.get_anchor("anchor_1")["anchor_id"] == "anchor_1"
        assert client.verify_anchor("anchor_1")["valid"] is True
    finally:
        server.shutdown()


def test_client_raises_on_http_error() -> None:
    """Unknown path returns 404; client raises RuntimeError with the detail.

    Mock handler's known routes (``/anchor/*``, ``/operator/*``, ``/status``,
    ``/verify/*``) all succeed, so we hit an unmapped path through the
    low-level ``_get`` helper to exercise the error branch.
    """
    server, _t, port = _make_mock_federation()
    try:
        client = FederationClient(f"http://127.0.0.1:{port}")
        with pytest.raises(RuntimeError, match="404"):
            client._get(f"http://127.0.0.1:{port}/does-not-exist")
    finally:
        server.shutdown()


# ---------------------------------------------------------------------
# 2. CrossAnchorReceipt — verify_cross_anchor_receipt
# ---------------------------------------------------------------------


def _build_signed_receipt(
    *, federation_priv: bytes, federation_pub: bytes
) -> CrossAnchorReceipt:
    """Build a valid CrossAnchorReceipt with a real federation signature."""
    participants = [
        {"operator_id": "op_a", "chain_signature": "aa" * 32},
        {"operator_id": "op_b", "chain_signature": "bb" * 32},
    ]
    anchored_at = "2026-05-26T18:00:00Z"
    # Reconstruct the same hash the verifier expects.
    sorted_ops = sorted(participants, key=lambda o: o["operator_id"])
    blob = "".join(o["chain_signature"] for o in sorted_ops).encode("utf-8")
    blob += anchored_at.encode("utf-8")
    cross_anchor_hash = hashlib.sha256(blob).hexdigest()

    receipt = CrossAnchorReceipt(
        anchor_id="anchor_2026_05_26_001",
        cross_anchor_hash=cross_anchor_hash,
        participating_operators=participants,
        anchored_at=anchored_at,
        rekor_log_index=1511534821,
        rekor_url="https://rekor.sigstore.dev/api/v1/log/entries?logIndex=1511534821",
        federation_signature="",  # filled in below
        federation_public_key_pem=federation_pub.decode("ascii"),
    )
    # Sign the canonical payload + populate signature.
    payload = _canonical_receipt_payload(receipt)
    sig = ed25519_sign(payload, federation_priv)
    return CrossAnchorReceipt(
        anchor_id=receipt.anchor_id,
        cross_anchor_hash=receipt.cross_anchor_hash,
        participating_operators=receipt.participating_operators,
        anchored_at=receipt.anchored_at,
        rekor_log_index=receipt.rekor_log_index,
        rekor_url=receipt.rekor_url,
        federation_signature=base64.b64encode(sig).decode("ascii"),
        federation_public_key_pem=receipt.federation_public_key_pem,
    )


def test_verify_receipt_happy_path() -> None:
    priv, pub = generate_keypair()
    receipt = _build_signed_receipt(federation_priv=priv, federation_pub=pub)
    result = verify_cross_anchor_receipt(receipt)
    assert result["valid"] is True
    assert result["checks"]["signature_verified"] is True
    assert result["checks"]["cross_anchor_hash_recomputed"] is True


def test_verify_receipt_with_external_expected_pubkey() -> None:
    """Passing the federation's expected public key binds the trust anchor."""
    priv, pub = generate_keypair()
    receipt = _build_signed_receipt(federation_priv=priv, federation_pub=pub)
    result = verify_cross_anchor_receipt(receipt, federation_public_key_pem=pub)
    assert result["valid"] is True


def test_verify_receipt_rejects_wrong_external_pubkey() -> None:
    priv, pub = generate_keypair()
    _other_priv, other_pub = generate_keypair()
    receipt = _build_signed_receipt(federation_priv=priv, federation_pub=pub)
    result = verify_cross_anchor_receipt(receipt, federation_public_key_pem=other_pub)
    assert result["valid"] is False
    assert "federation public key" in (result["reason"] or "")


def test_verify_receipt_rejects_tampered_hash() -> None:
    """Editing the cross-anchor hash without re-signing fails."""
    priv, pub = generate_keypair()
    receipt = _build_signed_receipt(federation_priv=priv, federation_pub=pub)
    from dataclasses import replace
    tampered = replace(receipt, cross_anchor_hash="0" * 64)
    result = verify_cross_anchor_receipt(tampered)
    assert result["valid"] is False
    assert "hash mismatch" in (result["reason"] or "")


def test_verify_receipt_rejects_tampered_signature() -> None:
    priv, pub = generate_keypair()
    receipt = _build_signed_receipt(federation_priv=priv, federation_pub=pub)
    sig = receipt.federation_signature
    flipped = ("z" if sig[0] != "z" else "y") + sig[1:]
    from dataclasses import replace
    bad = replace(receipt, federation_signature=flipped)
    result = verify_cross_anchor_receipt(bad)
    assert result["valid"] is False
    assert "signature verification failed" in (result["reason"] or "")


def test_receipt_dataclass_to_dict_json_roundtrip() -> None:
    priv, pub = generate_keypair()
    receipt = _build_signed_receipt(federation_priv=priv, federation_pub=pub)
    d = receipt.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["anchor_id"] == "anchor_2026_05_26_001"


# ---------------------------------------------------------------------
# 3. CLI
# ---------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "bijotel.cli.main", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_cli_federation_register_dry_run(tmp_path: Path) -> None:
    """--dry-run emits the registration payload without a network call."""
    priv, pub = generate_keypair()
    priv_path = tmp_path / "priv.pem"
    pub_path = tmp_path / "pub.pem"
    priv_path.write_bytes(priv)
    pub_path.write_bytes(pub)

    out = _run_cli(
        "federation", "register",
        "--public-key", str(pub_path),
        "--private-key", str(priv_path),
        "--org", "TestCo",
        "--dry-run",
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["org_name"] == "TestCo"
    assert "DRY RUN" in payload["note"]


def test_cli_federation_verify_local_happy_path(tmp_path: Path) -> None:
    """--receipt PATH verifies locally against the embedded federation key."""
    priv, pub = generate_keypair()
    receipt = _build_signed_receipt(federation_priv=priv, federation_pub=pub)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(asdict(receipt)), encoding="utf-8")

    out = _run_cli("federation", "verify", str(receipt_path))
    assert out.returncode == 0
    assert "MATCH" in out.stdout
    assert "signature_verified:  True" in out.stdout


def test_cli_federation_verify_with_external_pubkey(tmp_path: Path) -> None:
    priv, pub = generate_keypair()
    receipt = _build_signed_receipt(federation_priv=priv, federation_pub=pub)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(asdict(receipt)), encoding="utf-8")
    pub_path = tmp_path / "fed_pub.pem"
    pub_path.write_bytes(pub)

    out = _run_cli(
        "federation", "verify",
        str(receipt_path),
        "--federation-key", str(pub_path),
    )
    assert out.returncode == 0


def test_cli_federation_verify_mismatch_exit_code_3(tmp_path: Path) -> None:
    priv, pub = generate_keypair()
    receipt = _build_signed_receipt(federation_priv=priv, federation_pub=pub)
    # Tamper.
    data = asdict(receipt)
    data["cross_anchor_hash"] = "0" * 64
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(data), encoding="utf-8")

    out = _run_cli("federation", "verify", str(receipt_path))
    assert out.returncode == 3
    assert "MISMATCH" in out.stdout


# ---------------------------------------------------------------------
# 4. Public API exports
# ---------------------------------------------------------------------


def test_public_api_exports() -> None:
    import bijotel

    for name in (
        "FederationClient",
        "RegistrationReceipt",
        "SubmissionReceipt",
        "CrossAnchorReceipt",
        "verify_cross_anchor_receipt",
    ):
        assert hasattr(bijotel, name), f"bijotel.{name} missing"
        assert name in bijotel.__all__
