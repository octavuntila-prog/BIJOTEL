"""Tests for F15 / Bijuteria #15 — Inference Routing layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from bijotel import PolicyEngine, ast_safety_check, prompt_pattern_deny
from bijotel.layers.routing import (
    DEFAULT_MODELS,
    Budget,
    ModelProfile,
    ModelRegistry,
    ParetoRouter,
    TaskClassifier,
    routing_recommendation,
)

# === TaskClassifier ===


def test_classify_empty_returns_zero() -> None:
    assert TaskClassifier().classify([]) == 0.0
    assert TaskClassifier().classify("") == 0.0


def test_classify_simple_prompt_low_score() -> None:
    score = TaskClassifier().classify("hello")
    assert score < 0.30, f"expected simple<0.30, got {score}"


def test_classify_long_prompt_higher_score() -> None:
    long_prompt = "a" * 2500
    short_score = TaskClassifier().classify("short")
    long_score = TaskClassifier().classify(long_prompt)
    assert long_score > short_score


def test_classify_code_block_boosts_score() -> None:
    plain = TaskClassifier().classify("Explain something to me here briefly")
    with_code = TaskClassifier().classify(
        "Explain this code:\n```python\nx = 1\n```"
    )
    assert with_code > plain


def test_classify_math_symbols_boost() -> None:
    plain = "Solve this: x + y = 10, x - y = 2"
    math_text = "Solve: ∫f(x)dx + ∑a_i ≤ ∞"
    assert TaskClassifier().classify(math_text) > TaskClassifier().classify(plain)


def test_classify_complexity_markers_boost() -> None:
    plain = TaskClassifier().classify("Tell me about this")
    complex_ = TaskClassifier().classify(
        "Analyze step by step and derive the trade-off, explain reasoning"
    )
    assert complex_ > plain


def test_classify_messages_list_shape() -> None:
    msgs = [{"role": "user", "content": "Analyze quantum entanglement step by step"}]
    score = TaskClassifier().classify(msgs)
    assert 0.0 < score <= 1.0


def test_classify_multipart_content() -> None:
    msgs = [{"role": "user", "content": [{"type": "text", "text": "Explain x"}]}]
    score = TaskClassifier().classify(msgs)
    assert score > 0


def test_classify_returns_in_unit_interval() -> None:
    """All inputs map to [0, 1]."""
    for txt in ["", "a", "a" * 10000, "explain analyze derive prove compare evaluate"]:
        score = TaskClassifier().classify(txt)
        assert 0.0 <= score <= 1.0


# === ModelRegistry ===


def test_registry_defaults_loaded() -> None:
    reg = ModelRegistry()
    assert "claude-haiku-4-5" in reg
    assert "claude-opus-4" in reg
    assert "gpt-4o-mini" in reg


def test_registry_add_custom_model() -> None:
    reg = ModelRegistry({})
    reg.add_model("my-model", ModelProfile(cost=0.10, quality=0.80, latency=0.40))
    assert "my-model" in reg
    assert reg.get("my-model").cost == 0.10


def test_registry_remove_model() -> None:
    reg = ModelRegistry()
    assert "claude-opus-4" in reg
    reg.remove_model("claude-opus-4")
    assert "claude-opus-4" not in reg


def test_registry_get_unknown_returns_none() -> None:
    assert ModelRegistry().get("nonexistent-model-xyz") is None


# === ParetoRouter ===


def test_router_simple_query_selects_cheap() -> None:
    """Complexity < 0.30 → cheapest usable model."""
    model = ParetoRouter().route(0.1)
    profile = DEFAULT_MODELS[model]
    # Should be the cheapest model with quality >= 0.60
    cheapest = min(
        p.cost for p in DEFAULT_MODELS.values() if p.quality >= 0.60
    )
    assert profile.cost == cheapest


def test_router_complex_query_selects_top_quality() -> None:
    model = ParetoRouter().route(0.95)
    profile = DEFAULT_MODELS[model]
    top_q = max(p.quality for p in DEFAULT_MODELS.values())
    assert profile.quality == top_q


def test_router_medium_query_balanced() -> None:
    """Medium complexity → best quality/cost ratio."""
    model = ParetoRouter().route(0.5)
    profile = DEFAULT_MODELS[model]
    assert 0.6 <= profile.quality < 1.0


def test_router_empty_registry_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        ParetoRouter(registry=ModelRegistry({})).route(0.5)


def test_router_complexity_clamped() -> None:
    """Out-of-range complexity values are clamped to [0, 1]."""
    r = ParetoRouter()
    assert r.route(-5.0) == r.route(0.0)
    assert r.route(99.0) == r.route(1.0)


# === Budget ===


@pytest.fixture
def budget_db(tmp_path: Path) -> Path:
    return tmp_path / "budget.db"


def test_budget_invalid_limit_raises(budget_db: Path) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        Budget(db_path=budget_db, daily_limit_usd=0)


def test_budget_can_spend_under_limit(budget_db: Path) -> None:
    b = Budget(db_path=budget_db, daily_limit_usd=10.0)
    assert b.can_spend(5.0) is True


def test_budget_cannot_spend_over_limit(budget_db: Path) -> None:
    b = Budget(db_path=budget_db, daily_limit_usd=10.0)
    b.record_spend(8.0)
    assert b.can_spend(5.0) is False  # 8 + 5 > 10
    assert b.can_spend(2.0) is True


def test_budget_spent_today_tracks(budget_db: Path) -> None:
    b = Budget(db_path=budget_db, daily_limit_usd=10.0)
    b.record_spend(2.5)
    b.record_spend(1.5)
    assert b.spent_today() == pytest.approx(4.0)


def test_budget_remaining_decreases(budget_db: Path) -> None:
    b = Budget(db_path=budget_db, daily_limit_usd=10.0)
    assert b.remaining() == 10.0
    b.record_spend(3.0)
    assert b.remaining() == pytest.approx(7.0)


def test_budget_persists_across_instances(budget_db: Path) -> None:
    b1 = Budget(db_path=budget_db, daily_limit_usd=10.0, agent_id="a1")
    b1.record_spend(4.0)
    b2 = Budget(db_path=budget_db, daily_limit_usd=10.0, agent_id="a1")
    assert b2.spent_today() == pytest.approx(4.0)


def test_budget_per_agent_isolated(budget_db: Path) -> None:
    a1 = Budget(db_path=budget_db, daily_limit_usd=10.0, agent_id="agent-A")
    a2 = Budget(db_path=budget_db, daily_limit_usd=10.0, agent_id="agent-B")
    a1.record_spend(7.0)
    assert a1.spent_today() == pytest.approx(7.0)
    assert a2.spent_today() == 0.0


def test_budget_negative_spend_ignored(budget_db: Path) -> None:
    b = Budget(db_path=budget_db, daily_limit_usd=10.0)
    b.record_spend(3.0)
    b.record_spend(-99.0)  # ignored
    assert b.spent_today() == pytest.approx(3.0)


def test_router_with_exhausted_budget_downgrades(budget_db: Path) -> None:
    """When budget is exhausted, router returns cheapest usable model."""
    b = Budget(db_path=budget_db, daily_limit_usd=1.0)
    b.record_spend(0.99)  # near-exhausted
    r = ParetoRouter()
    # Complex query that would normally route to Opus
    model = r.route(0.95, budget=b, estimated_call_cost=0.10)
    profile = DEFAULT_MODELS[model]
    # Should downgrade to cheap fallback (not Opus)
    assert profile.cost < 1.0


# === routing_recommendation policy rule ===


def test_routing_rule_invalid_mode_raises() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        routing_recommendation(mode="block")


def test_routing_rule_allows_optimal_choice() -> None:
    """Request matching recommended model → allow."""
    rule = routing_recommendation(mode="warn")
    # Simple prompt, request matches cheap model
    cheap = min(DEFAULT_MODELS, key=lambda m: DEFAULT_MODELS[m].cost)
    decision = rule({"model": cheap, "messages": [{"role": "user", "content": "hi"}]})
    assert decision.is_allow


def test_routing_rule_warns_on_overprovisioning() -> None:
    """Simple prompt + Opus → warn (over-provisioned)."""
    rule = routing_recommendation(mode="warn")
    decision = rule(
        {"model": "claude-opus-4", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert decision.is_warn
    assert "over-provisioned" in (decision.reason or "")


def test_routing_rule_unknown_model_allows() -> None:
    """Unknown model → allow (can't compare profiles fairly)."""
    rule = routing_recommendation(mode="warn")
    decision = rule(
        {"model": "unknown-model", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert decision.is_allow


def test_routing_combines_with_f11_and_ast() -> None:
    """Routing + prompt_pattern_deny + AST safety compose in PolicyEngine."""
    engine = PolicyEngine(
        [
            prompt_pattern_deny(mode="warn"),
            ast_safety_check(mode="warn"),
            routing_recommendation(mode="warn"),
        ]
    )
    # Simple benign prompt with Opus = only routing warns
    decision, warnings = engine.evaluate(
        {"model": "claude-opus-4", "messages": [{"role": "user", "content": "hi"}]}
    )
    rules = {w.rule for w in warnings}
    assert "routing_recommendation" in rules
