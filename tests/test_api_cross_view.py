"""Tests for ``POST /cross-view`` — unified read-only view across chains.

Exposes the ``bijotel cross-view`` CLI capability over REST: per-ecosystem
stats + optional STRUCTURAL integrity. Each chain stays sovereign (no merge).
NOT federation — this is the local ``bijotel.cross_view`` aggregator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from bijotel.api import create_app  # noqa: E402
from bijotel.processors import HmacChainSpanProcessor  # noqa: E402

SECRET = b"x" * 32


def _seed(db: Path, n: int) -> None:
    """Seed a chain.db with n sealed gen_ai spans (own provider, no global set)."""
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    tracer = provider.get_tracer("cross-view-test")
    for i in range(n):
        with tracer.start_as_current_span(f"s{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
            span.set_attribute("gen_ai.system", "anthropic")
            span.set_attribute("gen_ai.usage.input_tokens", 10)
            span.set_attribute("gen_ai.usage.output_tokens", 5)
    provider.shutdown()


def _client() -> TestClient:
    return TestClient(create_app(db_path="/tmp/no-such-cross.db"))


def test_cross_view_aggregates_two_chains(tmp_path: Path) -> None:
    gena = tmp_path / "gena.db"
    ara = tmp_path / "ara.db"
    _seed(gena, 6)
    _seed(ara, 3)

    r = _client().post(
        "/cross-view",
        json={
            "chains": [
                {"name": "GENA", "path": str(gena)},
                {"name": "ARA", "path": str(ara)},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ecosystems"] == 2
    assert body["total_entries"] == 9
    assert set(body["per_ecosystem"].keys()) == {"GENA", "ARA"}
    assert body["per_ecosystem"]["GENA"]["entries"] == 6
    assert body["per_ecosystem"]["ARA"]["entries"] == 3
    assert body["integrity_report"] is None  # not requested


def test_cross_view_integrity_is_structural_over_rest(tmp_path: Path) -> None:
    gena = tmp_path / "gena.db"
    _seed(gena, 4)
    r = _client().post(
        "/cross-view",
        json={"chains": [{"name": "GENA", "path": str(gena)}], "integrity": True},
    )
    assert r.status_code == 200, r.text
    rep = r.json()["integrity_report"]
    assert rep is not None
    assert rep["per_chain"]["GENA"]["valid"] is True
    # No HMAC secret crosses the wire -> structural check only.
    assert rep["per_chain"]["GENA"]["method"] == "structural"


def test_cross_view_missing_path_404(tmp_path: Path) -> None:
    r = _client().post(
        "/cross-view",
        json={"chains": [{"name": "X", "path": str(tmp_path / "nope.db")}]},
    )
    assert r.status_code == 404


def test_cross_view_duplicate_name_400(tmp_path: Path) -> None:
    gena = tmp_path / "gena.db"
    _seed(gena, 2)
    r = _client().post(
        "/cross-view",
        json={
            "chains": [
                {"name": "DUP", "path": str(gena)},
                {"name": "DUP", "path": str(gena)},
            ]
        },
    )
    assert r.status_code == 400


def test_cross_view_empty_chains_422(tmp_path: Path) -> None:
    # min_length=1 on chains -> Pydantic validation error (422).
    r = _client().post("/cross-view", json={"chains": []})
    assert r.status_code == 422
