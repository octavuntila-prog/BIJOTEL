"""Tests pentru built-in policy rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from bijotel.policy.rules import (
    cost_per_call_max,
    daily_token_budget,
    model_allowlist,
)

# === cost_per_call_max ===


def test_cost_per_call_max_allows_cheap_call() -> None:
    rule = cost_per_call_max(usd=10.0)
    request = {
        "model": "claude-haiku-4-5-20251001",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
    }
    assert rule(request).is_allow


def test_cost_per_call_max_denies_expensive_call() -> None:
    rule = cost_per_call_max(usd=0.0001)
    request = {
        "model": "claude-opus-4-7",  # Expensive
        "messages": [{"role": "user", "content": "x" * 10000}],
        "max_tokens": 4000,
    }
    decision = rule(request)
    assert decision.is_deny
    assert "exceeds limit" in decision.reason


def test_cost_per_call_max_unknown_model_denies() -> None:
    rule = cost_per_call_max(usd=10.0)
    request = {
        "model": "unknown-model-xyz",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
    }
    decision = rule(request)
    assert decision.is_deny
    assert "not in price table" in decision.reason


def test_cost_per_call_max_warn_mode() -> None:
    rule = cost_per_call_max(usd=0.0001, mode="warn")
    request = {
        "model": "claude-opus-4-7",
        "messages": [{"role": "user", "content": "x" * 10000}],
        "max_tokens": 4000,
    }
    decision = rule(request)
    assert decision.is_warn


def test_cost_per_call_max_custom_prices() -> None:
    custom = {"my-model": {"input": 0.001, "output": 0.005}}
    rule = cost_per_call_max(usd=10.0, prices=custom)
    request = {
        "model": "my-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 10,
    }
    assert rule(request).is_allow


def test_cost_per_call_max_invalid_mode() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        cost_per_call_max(usd=10.0, mode="invalid")


# === model_allowlist ===


def test_model_allowlist_allows_in_list() -> None:
    rule = model_allowlist("claude-haiku-4-5", "claude-sonnet-4-6")
    assert rule({"model": "claude-haiku-4-5"}).is_allow


def test_model_allowlist_denies_not_in_list() -> None:
    rule = model_allowlist("claude-haiku-4-5")
    decision = rule({"model": "claude-opus-4-7"})
    assert decision.is_deny
    assert "not in allowlist" in decision.reason


def test_model_allowlist_warn_mode() -> None:
    rule = model_allowlist("claude-haiku-4-5", mode="warn")
    decision = rule({"model": "claude-opus-4-7"})
    assert decision.is_warn


# === daily_token_budget ===


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "policy.db"


def test_daily_budget_allows_under_limit(db_path: Path) -> None:
    rule = daily_token_budget(tokens=10000, db_path=db_path)
    request = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    assert rule(request).is_allow


def test_daily_budget_denies_over_limit(db_path: Path) -> None:
    rule = daily_token_budget(tokens=100, db_path=db_path)
    # First call: ~5+50 tokens, OK
    rule({"messages": [{"role": "user", "content": "x"}], "max_tokens": 50})
    # Second call: pushes total over 100
    decision = rule(
        {"messages": [{"role": "user", "content": "y"}], "max_tokens": 50}
    )
    assert decision.is_deny
    assert "budget" in decision.reason


def test_daily_budget_persists_across_rule_instances(db_path: Path) -> None:
    """State persisted in SQLite, survives function recreation."""
    rule1 = daily_token_budget(tokens=200, db_path=db_path)
    rule1({"messages": [{"role": "user", "content": "x"}], "max_tokens": 100})

    # Recreate rule (simulates restart)
    rule2 = daily_token_budget(tokens=200, db_path=db_path)
    decision = rule2(
        {"messages": [{"role": "user", "content": "y"}], "max_tokens": 150}
    )
    assert decision.is_deny  # Cumulative > 200


def test_daily_budget_warn_mode(db_path: Path) -> None:
    rule = daily_token_budget(tokens=10, db_path=db_path, mode="warn")
    decision = rule(
        {"messages": [{"role": "user", "content": "x"}], "max_tokens": 100}
    )
    assert decision.is_warn
