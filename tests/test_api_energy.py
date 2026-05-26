"""Tests for ``/energy/*`` routes (Bijuteria #3 — v1.9.0).

Coverage gap caught by internal audit 2026-05-26: the route shipped in
v1.9.0 but no dedicated test file existed. Coverage on the route was
39% — only ``__init__`` and signatures exercised via import.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from bijotel.api import create_app  # noqa: E402

# ───────────────────────── /energy/estimate ─────────────────────────


def test_estimate_returns_200_for_haiku() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    r = client.post(
        "/energy/estimate",
        json={
            "model": "claude-haiku-4-5-20251001",
            "tokens_in": 1000,
            "tokens_out": 500,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["tokens"] == 1500
    assert body["wh"] > 0
    assert body["co2_grams"] >= 0


def test_estimate_default_region_us_east() -> None:
    """When region is omitted, default carbon calc still works."""
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    r = client.post(
        "/energy/estimate",
        json={"model": "claude-sonnet-4-5-20250929", "tokens_in": 100, "tokens_out": 50},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["region"] is not None
    assert body["intensity_g_per_kwh"] > 0


def test_estimate_with_explicit_region() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    r = client.post(
        "/energy/estimate",
        json={
            "model": "claude-haiku-4-5-20251001",
            "tokens_in": 100,
            "tokens_out": 50,
            "region": "eu-west",
        },
    )
    assert r.status_code == 200
    assert r.json()["region"] == "eu-west"


def test_estimate_unknown_model_falls_back_to_default() -> None:
    """An unknown model uses the fallback Wh/token estimate."""
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    r = client.post(
        "/energy/estimate",
        json={"model": "unknown-model-xyz", "tokens_in": 100, "tokens_out": 50},
    )
    assert r.status_code == 200
    assert r.json()["wh"] > 0


def test_estimate_zero_tokens_is_zero_energy() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    r = client.post(
        "/energy/estimate",
        json={"model": "claude-haiku-4-5-20251001", "tokens_in": 0, "tokens_out": 0},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["wh"] == 0
    assert body["co2_grams"] == 0


def test_estimate_missing_tokens_field_400() -> None:
    """Pydantic rejects malformed payload with 422."""
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    r = client.post(
        "/energy/estimate",
        json={"model": "claude-haiku-4-5-20251001"},  # missing tokens
    )
    assert r.status_code == 422


# ───────────────────────── /energy/summary ─────────────────────────


@pytest.fixture
def energy_db(tmp_path: Path) -> Path:
    """Build a chain.db with energy_log entries via the EnergyTracker."""
    from bijotel.layers.energy import EnergyTracker
    db = tmp_path / "chain.db"
    # Minimal chain table so EnergyTracker can attach.
    import sqlite3
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE chain (seq INTEGER PRIMARY KEY, timestamp_ns INTEGER)"
        )
    tracker = EnergyTracker(db)
    tracker.record(
        model="claude-haiku-4-5-20251001",
        tokens_in=100, tokens_out=50,
        timestamp_ns=1_700_000_000_000_000_000,
        agent_id="test-agent",
        span_seq=1,
    )
    tracker.record(
        model="claude-sonnet-4-5-20250929",
        tokens_in=500, tokens_out=200,
        timestamp_ns=1_700_001_000_000_000_000,
        agent_id="test-agent",
        span_seq=2,
    )
    return db


def test_summary_returns_200_with_data(energy_db: Path) -> None:
    app = create_app(db_path=str(energy_db))
    client = TestClient(app)
    r = client.get("/energy/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 2
    assert body["total_tokens"] == 850
    assert body["total_wh"] > 0
    assert body["total_co2_grams"] >= 0
    assert body["has_data"] is True


def test_summary_per_model_breakdown(energy_db: Path) -> None:
    app = create_app(db_path=str(energy_db))
    client = TestClient(app)
    r = client.get("/energy/summary")
    body = r.json()
    models = {m["model"]: m for m in body["per_model"]}
    assert "claude-haiku-4-5-20251001" in models
    assert "claude-sonnet-4-5-20250929" in models
    assert models["claude-haiku-4-5-20251001"]["calls"] == 1
    assert models["claude-sonnet-4-5-20250929"]["calls"] == 1


def test_summary_per_agent_breakdown(energy_db: Path) -> None:
    app = create_app(db_path=str(energy_db))
    client = TestClient(app)
    r = client.get("/energy/summary")
    body = r.json()
    agents = {a["agent_id"]: a for a in body["per_agent"]}
    assert "test-agent" in agents
    assert agents["test-agent"]["calls"] == 2


def test_summary_agent_id_filter(energy_db: Path) -> None:
    """Filtering by agent_id narrows the result set."""
    app = create_app(db_path=str(energy_db))
    client = TestClient(app)
    r = client.get("/energy/summary?agent_id=test-agent")
    assert r.status_code == 200
    assert r.json()["total_calls"] == 2

    r2 = client.get("/energy/summary?agent_id=nonexistent")
    assert r2.status_code == 200
    assert r2.json()["total_calls"] == 0
    assert r2.json()["has_data"] is False


def test_summary_lazy_builds_tracker_when_db_provided() -> None:
    """Without an explicit EnergyTracker but with db_path, /summary
    lazy-builds a tracker against the chain DB rather than 503-ing.
    This is the documented fallback in routes/energy.py."""
    import sqlite3
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    # Minimal chain table — empty.
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE chain (seq INTEGER PRIMARY KEY, timestamp_ns INTEGER)")
    app = create_app(db_path=db_path)
    client = TestClient(app)
    r = client.get("/energy/summary")
    # Empty energy_log → has_data False, total_calls 0, but the endpoint
    # itself returns 200 (lazy-built tracker handled it).
    assert r.status_code == 200
    body = r.json()
    assert body["total_calls"] == 0
    assert body["has_data"] is False


def test_summary_carbon_equivalents_populated(energy_db: Path) -> None:
    """Summary returns human-friendly equivalents (km, phone, kettle)."""
    app = create_app(db_path=str(energy_db))
    client = TestClient(app)
    body = client.get("/energy/summary").json()
    assert "equivalent_km_driven" in body
    assert "equivalent_phone_charges" in body
    assert "equivalent_kettle_boils" in body
    assert body["equivalent_km_driven"] >= 0
