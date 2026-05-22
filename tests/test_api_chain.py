"""Tests for ``/chain/*`` routes (Day 6 / v1.1.0 part 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

# Skip the entire module without [api] extra installed
fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from bijotel.api import create_app  # noqa: E402
from bijotel.processors import HmacChainSpanProcessor  # noqa: E402

SECRET = b"x" * 32
SECRET_HEX = SECRET.hex()


# ───────────────────────── fixtures ─────────────────────────


@pytest.fixture
def chain_db(tmp_path: Path) -> Path:
    """Build a chain.db with 5 sealed spans for use by tests."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test_api_chain")
    for i in range(5):
        with tracer.start_as_current_span(f"test-span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 10 + i)
            span.set_attribute("gen_ai.usage.output_tokens", 5)

    provider.shutdown()
    return db


@pytest.fixture
def client(chain_db: Path, monkeypatch) -> TestClient:
    """TestClient with secret in env so /verify can do full mode."""
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(chain_db))
    return TestClient(app)


@pytest.fixture
def client_no_secret(chain_db: Path, monkeypatch) -> TestClient:
    """TestClient WITHOUT secret in env — for testing hmac_valid=null path."""
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)
    app = create_app(db_path=str(chain_db))
    return TestClient(app)


# ───────────────────────── GET /chain ─────────────────────────


def test_chain_list_default(client: TestClient) -> None:
    r = client.get("/chain")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert len(body["entries"]) == 5
    assert body["pagination"]["limit"] == 50
    assert body["pagination"]["offset"] == 0
    assert body["pagination"]["has_more"] is False


def test_chain_list_with_limit(client: TestClient) -> None:
    r = client.get("/chain?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert len(body["entries"]) == 2
    assert body["pagination"]["has_more"] is True


def test_chain_list_with_offset(client: TestClient) -> None:
    r = client.get("/chain?limit=2&offset=3")
    assert r.status_code == 200
    body = r.json()
    assert len(body["entries"]) == 2
    assert body["pagination"]["offset"] == 3
    assert body["pagination"]["has_more"] is False


def test_chain_list_invalid_limit_rejected(client: TestClient) -> None:
    r = client.get("/chain?limit=9999")  # > 500 max
    assert r.status_code == 422


def test_chain_list_invalid_since_format(client: TestClient) -> None:
    r = client.get("/chain?since=not-a-date")
    assert r.status_code == 400
    assert "Invalid 'since'" in r.json()["detail"]


def test_chain_list_since_filter(client: TestClient) -> None:
    """Lower-bound on timestamp filters out rows (use far-future filter)."""
    r = client.get("/chain?since=2099-01-01T00:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["entries"] == []


def test_chain_list_hmac_valid_with_secret(client: TestClient) -> None:
    """With BIJOTEL_HMAC_SECRET set, every entry reports a real hmac_valid bool."""
    r = client.get("/chain")
    body = r.json()
    for e in body["entries"]:
        assert e["hmac_valid"] is True


def test_chain_list_db_missing_returns_503(tmp_path: Path) -> None:
    app = create_app(db_path=str(tmp_path / "nope.db"))
    client = TestClient(app)
    r = client.get("/chain")
    assert r.status_code == 503
    assert "Chain DB not found" in r.json()["detail"]


# ───────────────────────── GET /chain/stats ─────────────────────────


def test_chain_stats(client: TestClient) -> None:
    r = client.get("/chain/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_entries"] == 5
    assert body["db_size_bytes"] > 0
    assert body["first_entry"] is not None
    assert body["last_entry"] is not None
    assert body["age_days"] >= 0


def test_chain_stats_empty_db(tmp_path: Path, monkeypatch) -> None:
    """Empty chain (no rows) — stats endpoint returns zeros, not 500."""
    db = tmp_path / "empty.db"
    # Initialize the chain table without writing any rows
    HmacChainSpanProcessor(db_path=db, secret_key=SECRET)
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(db))
    client = TestClient(app)
    r = client.get("/chain/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_entries"] == 0
    assert body["entries_per_day"] == 0.0


# ───────────────────────── GET /chain/{seq} ─────────────────────────


def test_chain_detail_exists(client: TestClient) -> None:
    r = client.get("/chain/1")
    assert r.status_code == 200
    body = r.json()
    assert body["seq"] == 1
    assert body["span_name"] == "test-span-0"
    assert isinstance(body["canonical_body"], dict)
    assert body["hmac_valid"] is True
    assert body["cas_ref"] is not None  # semantic_body_hash filled


def test_chain_detail_not_found(client: TestClient) -> None:
    r = client.get("/chain/9999")
    assert r.status_code == 404


def test_chain_detail_invalid_seq(client: TestClient) -> None:
    r = client.get("/chain/0")
    assert r.status_code == 400  # seq must be >= 1


def test_chain_detail_without_secret_hmac_unknown(
    client_no_secret: TestClient,
) -> None:
    """No env secret → hmac_valid defaults to false (can't verify)."""
    r = client_no_secret.get("/chain/1")
    assert r.status_code == 200
    assert r.json()["hmac_valid"] is False


# ───────────────────────── POST /chain/verify ─────────────────────────


def test_chain_verify_smoke_default(client: TestClient) -> None:
    r = client.post("/chain/verify")  # body omitted → smoke
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["first_seq"] == 1
    assert body["last_seq"] == 5


def test_chain_verify_full_with_secret(client: TestClient) -> None:
    r = client.post("/chain/verify", json={"full": True})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["entries_verified"] == 5
    assert body["error"] is None


def test_chain_verify_full_without_secret_400(client_no_secret: TestClient) -> None:
    r = client_no_secret.post("/chain/verify", json={"full": True})
    assert r.status_code == 400
    assert "BIJOTEL_HMAC_SECRET" in r.json()["detail"]


def test_chain_verify_empty_chain_valid(tmp_path: Path, monkeypatch) -> None:
    """Empty chain verifies as valid (no rows = nothing broken)."""
    db = tmp_path / "empty.db"
    HmacChainSpanProcessor(db_path=db, secret_key=SECRET)
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(db))
    client = TestClient(app)
    r = client.post("/chain/verify", json={"full": True})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    assert body["entries_verified"] == 0
