"""Tests for ``/export`` + ``/export/verify`` routes (Day 7 / v1.1.0)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from bijotel.api import create_app  # noqa: E402
from bijotel.processors import HmacChainSpanProcessor  # noqa: E402

SECRET = b"x" * 32
SECRET_HEX = SECRET.hex()


@pytest.fixture
def chain_db(tmp_path: Path) -> Path:
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test_api_export")
    for i in range(4):
        with tracer.start_as_current_span(f"span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 10 + i)
            span.set_attribute("gen_ai.usage.output_tokens", 5)
    provider.shutdown()
    return db


@pytest.fixture
def client(chain_db: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(chain_db))
    return TestClient(app)


# ───────────────────────── POST /export ─────────────────────────


def test_export_returns_json_attachment(client: TestClient) -> None:
    r = client.post("/export")
    assert r.status_code == 200
    # Content-Disposition: attachment; filename=bijotel-export-...
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "bijotel-export-" in cd
    assert r.headers["content-type"].startswith("application/json")


def test_export_body_is_valid_bijotel_chain_v1(client: TestClient) -> None:
    r = client.post("/export")
    data = json.loads(r.content)
    assert data["format"] == "bijotel-chain-v1"
    assert data["entries_count"] == 4
    assert len(data["entries"]) == 4
    assert "chain_signature" in data
    assert "head_hash" in data


def test_export_without_secret_400(chain_db: Path, monkeypatch) -> None:
    """No BIJOTEL_HMAC_SECRET → 400 with remediation message."""
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)
    app = create_app(db_path=str(chain_db))
    c = TestClient(app)
    r = c.post("/export")
    assert r.status_code == 400
    assert "BIJOTEL_HMAC_SECRET" in r.json()["detail"]


def test_export_db_missing_503(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(tmp_path / "nope.db"))
    c = TestClient(app)
    r = c.post("/export")
    assert r.status_code == 503


# ───────────────────────── POST /export/verify ─────────────────────────


def test_export_verify_roundtrip(client: TestClient) -> None:
    """Export then immediately verify — should report valid."""
    exported = client.post("/export").content

    r = client.post(
        "/export/verify",
        files={"file": ("audit.json", exported, "application/json")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["entries_count"] == 4
    assert body["format"] == "bijotel-chain-v1"
    assert body["head_hash"] is not None
    assert body["reason"] is None


def test_export_verify_tampered_chain_signature(client: TestClient) -> None:
    """Flipping chain_signature → verify reports invalid."""
    raw = client.post("/export").content
    data = json.loads(raw)
    data["chain_signature"] = "0" * 64
    tampered = json.dumps(data).encode()

    r = client.post(
        "/export/verify",
        files={"file": ("audit.json", tampered, "application/json")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] is not None
    # Still reports metadata fields where possible
    assert body["entries_count"] == 4


def test_export_verify_tampered_entry(client: TestClient) -> None:
    raw = client.post("/export").content
    data = json.loads(raw)
    # Mutate first entry's canonical_hash
    data["entries"][0]["canonical_hash"] = "0" * 64
    tampered = json.dumps(data).encode()
    r = client.post(
        "/export/verify",
        files={"file": ("audit.json", tampered, "application/json")},
    )
    body = r.json()
    assert body["valid"] is False
    assert body["reason"] is not None


def test_export_verify_wrong_secret_400(chain_db: Path, monkeypatch) -> None:
    """Server with a DIFFERENT secret can't verify a foreign export.

    But /export/verify itself returns 200 with valid=false — the 400
    branch is reserved for missing-env-var (no secret at all). Here we
    confirm that mismatched secrets surface as valid=false, not as
    a server error.
    """
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(chain_db))
    c = TestClient(app)
    exported = c.post("/export").content

    # Switch env to a different secret BEFORE the verify call
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", (b"y" * 32).hex())
    app2 = create_app(db_path=str(chain_db))
    c2 = TestClient(app2)
    r = c2.post(
        "/export/verify",
        files={"file": ("audit.json", exported, "application/json")},
    )
    body = r.json()
    assert body["valid"] is False
    assert "mismatch" in (body["reason"] or "").lower()


def test_export_verify_without_secret_400(chain_db: Path, monkeypatch) -> None:
    """Verify also needs the secret in env (to know what to verify against)."""
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)
    app = create_app(db_path=str(chain_db))
    c = TestClient(app)
    r = c.post(
        "/export/verify",
        files={"file": ("audit.json", b'{"format":"x"}', "application/json")},
    )
    assert r.status_code == 400
    assert "BIJOTEL_HMAC_SECRET" in r.json()["detail"]
