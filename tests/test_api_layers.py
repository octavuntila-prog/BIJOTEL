"""Tests for ``/layers`` route (Day 6 / v1.1.0 part 1)."""

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
def populated_db(tmp_path: Path) -> Path:
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test_api_layers")
    for i in range(6):  # >=5 so regression marks active
        # HmacChainSpanProcessor filters spans without GenAI attrs — set them
        # so the chain actually seals them.
        with tracer.start_as_current_span(f"span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 10 + i)
            span.set_attribute("gen_ai.usage.output_tokens", 5)
    provider.shutdown()
    return db


def test_layers_endpoint_returns_envelope() -> None:
    app = create_app(db_path="/tmp/no-such-chain.db")
    client = TestClient(app)
    r = client.get("/layers")
    assert r.status_code == 200
    body = r.json()
    # Always has the envelope keys
    for k in ("layers", "total", "active", "available", "planned"):
        assert k in body
    assert body["total"] == len(body["layers"])
    assert (
        body["active"] + body["available"] + body["planned"] == body["total"]
    )


def test_layers_count_matches_manifest() -> None:
    """The manifest currently has 14 entries (10 wired + 2 active default + 2 planned)."""
    app = create_app(db_path="/tmp/no-such-chain.db")
    client = TestClient(app)
    body = client.get("/layers").json()
    assert body["total"] == 14  # bumped if/when new bijuterii are added
    assert body["planned"] == 2  # energy + consensus


def test_layers_planned_set() -> None:
    app = create_app(db_path="/tmp/no-such-chain.db")
    client = TestClient(app)
    body = client.get("/layers").json()
    planned_ids = {layer["id"] for layer in body["layers"] if layer["status"] == "planned"}
    assert planned_ids == {"energy", "consensus"}


def test_layers_chain_active_when_db_populated(populated_db: Path) -> None:
    app = create_app(db_path=str(populated_db))
    client = TestClient(app)
    body = client.get("/layers").json()
    chain_layer = next(layer for layer in body["layers"] if layer["id"] == "forensic_chain")
    assert chain_layer["status"] == "active"
    assert chain_layer["metrics"]["entries"] >= 6


def test_layers_regression_ready_when_enough_baseline(populated_db: Path) -> None:
    """Regression marks active when chain has >= MIN_SAMPLES (5)."""
    app = create_app(db_path=str(populated_db))
    client = TestClient(app)
    body = client.get("/layers").json()
    reg = next(layer for layer in body["layers"] if layer["id"] == "regression")
    assert reg["status"] == "active"
    assert reg["metrics"]["ready"] is True


def test_layers_extras_detection() -> None:
    """fingerprint + ast layers reflect whether the extras are installed."""
    app = create_app(db_path="/tmp/no-such-chain.db")
    client = TestClient(app)
    body = client.get("/layers").json()
    fp = next(layer for layer in body["layers"] if layer["id"] == "fingerprint")
    ast = next(layer for layer in body["layers"] if layer["id"] == "ast_safety")
    # The metric reports a bool either way — just confirm key presence + type
    assert isinstance(fp["metrics"]["sentence_transformers"], bool)
    assert isinstance(ast["metrics"]["tree_sitter"], bool)


def test_layers_bijuterii_references_present() -> None:
    """Every layer entry carries a `bijuterie` identifier (#N or 'Combo D')."""
    app = create_app(db_path="/tmp/no-such-chain.db")
    client = TestClient(app)
    body = client.get("/layers").json()
    for layer in body["layers"]:
        assert layer["bijuterie"]  # non-empty
        assert isinstance(layer["bijuterie"], str)
