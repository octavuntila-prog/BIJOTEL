"""Tests for ``/regression/*`` routes (Day 7 / v1.1.0)."""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from bijotel.api import create_app  # noqa: E402
from bijotel.processors import HmacChainSpanProcessor  # noqa: E402

SECRET = b"x" * 32


@pytest.fixture
def chain_with_signal(tmp_path: Path) -> Path:
    """Build a chain with ~20 spans of consistent tokens — clean baseline."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test_api_regression")
    for i in range(20):
        with tracer.start_as_current_span(f"span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            # tight distribution (mean ~100, very low stdev)
            span.set_attribute("gen_ai.usage.input_tokens", 100 + (i % 3))
            span.set_attribute("gen_ai.usage.output_tokens", 50 + (i % 2))
    provider.shutdown()
    return db


@pytest.fixture
def client(chain_with_signal: Path) -> TestClient:
    app = create_app(db_path=str(chain_with_signal))
    return TestClient(app)


# ───────────────────────── POST /regression/run ─────────────────────────


def test_regression_run_persists_by_default(client: TestClient) -> None:
    r = client.post("/regression/run", json={"window": 10, "z_threshold": 3.0})
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] is not None  # persisted
    assert body["window"] == 10
    assert body["status"] in ("clean", "anomaly", "insufficient_data")
    # All 3 dimensions present
    assert set(body["dimensions"].keys()) == {"input_tokens", "output_tokens", "cost"}


def test_regression_run_no_persist(client: TestClient) -> None:
    r = client.post(
        "/regression/run",
        json={"window": 10, "z_threshold": 3.0, "persist": False},
    )
    body = r.json()
    assert body["run_id"] is None  # not persisted


def test_regression_run_default_body(client: TestClient) -> None:
    """Body omitted → defaults window=100 z=3.0 persist=true."""
    r = client.post("/regression/run")
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == 100
    assert body["z_threshold"] == 3.0
    assert body["run_id"] is not None


def test_regression_run_invalid_window(client: TestClient) -> None:
    r = client.post("/regression/run", json={"window": 0})  # < min 5
    assert r.status_code == 422


def test_regression_run_db_missing_503(tmp_path: Path) -> None:
    app = create_app(db_path=str(tmp_path / "nope.db"))
    c = TestClient(app)
    r = c.post("/regression/run")
    assert r.status_code == 503


# ───────────────────────── GET /regression/latest ─────────────────────────


def test_regression_latest_404_when_no_runs(chain_with_signal: Path) -> None:
    app = create_app(db_path=str(chain_with_signal))
    c = TestClient(app)
    r = c.get("/regression/latest")
    assert r.status_code == 404
    assert "No regression runs persisted" in r.json()["detail"]


def test_regression_latest_after_run(client: TestClient) -> None:
    client.post("/regression/run", json={"window": 10})
    r = client.get("/regression/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] is not None
    assert "dimensions" in body


# ───────────────────────── GET /regression/history ─────────────────────────


def test_regression_history_empty(chain_with_signal: Path) -> None:
    app = create_app(db_path=str(chain_with_signal))
    c = TestClient(app)
    r = c.get("/regression/history")
    assert r.status_code == 200
    body = r.json()
    assert body["total_runs"] == 0
    assert body["runs"] == []


def test_regression_history_accumulates(client: TestClient) -> None:
    for _ in range(3):
        client.post("/regression/run", json={"window": 10})
    r = client.get("/regression/history")
    body = r.json()
    assert body["total_runs"] == 3
    assert len(body["runs"]) == 3
    # Sorted DESC by id — most recent first
    ids = [x["run_id"] for x in body["runs"]]
    assert ids == sorted(ids, reverse=True)


def test_regression_history_pagination(client: TestClient) -> None:
    for _ in range(5):
        client.post("/regression/run", json={"window": 10})
    r = client.get("/regression/history?limit=2&offset=1")
    body = r.json()
    assert body["total_runs"] == 5
    assert len(body["runs"]) == 2
