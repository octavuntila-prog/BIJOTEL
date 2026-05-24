"""Tests for ``/consensus/*`` routes (Bijuteria #9 — v1.8.0)."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from bijotel.api import create_app  # noqa: E402
from bijotel.layers.consensus import ModelResponse  # noqa: E402

# ───────────────────────── stakes endpoint ─────────────────────────


def test_stakes_high_medical() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    r = client.post(
        "/consensus/stakes",
        json={
            "messages": [
                {"role": "user", "content": "What medication for my diagnosis?"}
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stakes"] == "high"
    assert "medication" in body["keywords_found"]
    assert "diagnosis" in body["keywords_found"]


def test_stakes_low_benign() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    r = client.post(
        "/consensus/stakes",
        json={"messages": [{"role": "user", "content": "hi friend"}]},
    )
    body = r.json()
    assert body["stakes"] == "low"
    assert body["keywords_found"] == []


def test_stakes_no_llm_call_required() -> None:
    """Stakes endpoint never needs an LLM provider."""
    app = create_app(db_path="/tmp/no-such.db")
    # explicitly null any provider
    app.state.consensus_provider = None
    client = TestClient(app)
    r = client.post(
        "/consensus/stakes",
        json={"messages": [{"role": "user", "content": "tax fraud"}]},
    )
    assert r.status_code == 200


# ───────────────────────── evaluate endpoint ─────────────────────────


def _attach_mock(app, responses_by_model: dict[str, str]) -> None:
    """Wire a deterministic provider on app.state for testing."""

    async def fake(model: str, messages: list[dict], max_tokens: int) -> ModelResponse:
        text = responses_by_model.get(model, "")
        return ModelResponse(
            model=model,
            response=text,
            tokens_in=10,
            tokens_out=len(text),
            cost_usd=0.001 if "haiku" in model.lower() else 0.004,
            latency_ms=10.0,
            error=None,
        )

    app.state.consensus_provider = fake


def test_evaluate_two_models_agree() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    _attach_mock(app, {"haiku": "Paris is the capital", "sonnet": "Paris is the capital"})
    client = TestClient(app)
    r = client.post(
        "/consensus/evaluate",
        json={
            "messages": [{"role": "user", "content": "Capital of France?"}],
            "models": ["haiku", "sonnet"],
            "threshold": 0.7,
        },
    )
    body = r.json()
    assert body["consensus_reached"] is True
    assert body["agreement_score"] == 1.0
    assert len(body["responses"]) == 2


def test_evaluate_two_models_disagree() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    _attach_mock(app, {"haiku": "alpha bravo", "sonnet": "delta echo"})
    client = TestClient(app)
    r = client.post(
        "/consensus/evaluate",
        json={
            "messages": [{"role": "user", "content": "Q"}],
            "models": ["haiku", "sonnet"],
            "threshold": 0.5,
        },
    )
    body = r.json()
    assert body["consensus_reached"] is False
    assert body["agreement_score"] < 0.5
    assert len(body["disagreement_details"]) >= 1


def test_evaluate_recommended_is_higher_cost() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    _attach_mock(app, {"haiku": "cheap reply", "sonnet": "premium reply"})
    client = TestClient(app)
    r = client.post(
        "/consensus/evaluate",
        json={
            "messages": [{"role": "user", "content": "Q"}],
            "models": ["haiku", "sonnet"],
        },
    )
    body = r.json()
    assert body["recommended_model"] == "sonnet"
    assert body["recommended_response"] == "premium reply"


def test_evaluate_503_when_no_provider_and_no_anthropic() -> None:
    """Without provider AND without anthropic installed → 503.

    This test is conditional: when anthropic IS installed locally
    (dev env), the default provider would attempt a real call. Skip in
    that case — the 503 path is the failure mode for clean installs.
    """
    try:
        import anthropic  # noqa: F401
        pytest.skip("anthropic SDK installed locally — 503 path not reachable here")
    except ImportError:
        pass

    app = create_app(db_path="/tmp/no-such.db")
    app.state.consensus_provider = None  # explicit
    client = TestClient(app)
    r = client.post(
        "/consensus/evaluate",
        json={"messages": [{"role": "user", "content": "Q"}], "models": ["m1"]},
    )
    assert r.status_code == 503


def test_evaluate_validates_models_non_empty() -> None:
    """Empty models list rejected by Pydantic (min_length=1)."""
    app = create_app(db_path="/tmp/no-such.db")
    _attach_mock(app, {})
    client = TestClient(app)
    r = client.post(
        "/consensus/evaluate",
        json={"messages": [{"role": "user", "content": "Q"}], "models": []},
    )
    assert r.status_code == 422


def test_evaluate_validates_threshold_bounds() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    _attach_mock(app, {})
    client = TestClient(app)
    r = client.post(
        "/consensus/evaluate",
        json={
            "messages": [{"role": "user", "content": "Q"}],
            "models": ["m1"],
            "threshold": 2.0,
        },
    )
    assert r.status_code == 422


def test_evaluate_cost_and_latency_in_response() -> None:
    app = create_app(db_path="/tmp/no-such.db")
    _attach_mock(app, {"haiku": "a", "sonnet": "b"})
    client = TestClient(app)
    r = client.post(
        "/consensus/evaluate",
        json={
            "messages": [{"role": "user", "content": "Q"}],
            "models": ["haiku", "sonnet"],
        },
    )
    body = r.json()
    assert body["cost_total_usd"] == pytest.approx(0.001 + 0.004, rel=1e-3)
    assert body["latency_ms"] > 0


def test_evaluate_per_model_error_surfaces() -> None:
    """One model failing → error in response.errors, others still succeed."""
    app = create_app(db_path="/tmp/no-such.db")

    async def flaky(model: str, messages, max_tokens) -> ModelResponse:
        if model == "broken":
            raise RuntimeError("upstream timeout")
        return ModelResponse(
            model=model, response="ok", tokens_in=1, tokens_out=1,
            cost_usd=0.001, latency_ms=5.0, error=None,
        )

    app.state.consensus_provider = flaky
    client = TestClient(app)
    r = client.post(
        "/consensus/evaluate",
        json={
            "messages": [{"role": "user", "content": "Q"}],
            "models": ["good", "broken"],
        },
    )
    body = r.json()
    assert len(body["errors"]) == 1
    assert "RuntimeError" in body["errors"][0]
    assert len(body["responses"]) == 2
