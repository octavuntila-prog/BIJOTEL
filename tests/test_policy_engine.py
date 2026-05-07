"""Tests pentru PolicyEngine."""

from __future__ import annotations

from bijotel.policy import Decision, PolicyEngine


def _allow_rule(_req: dict) -> Decision:
    return Decision.allow()


def _deny_rule(_req: dict) -> Decision:
    return Decision.deny(rule="deny_test", reason="denied")


def _warn_rule(_req: dict) -> Decision:
    return Decision.warn(rule="warn_test", reason="warned")


def test_engine_all_allow() -> None:
    engine = PolicyEngine([_allow_rule, _allow_rule])
    decision, warnings = engine.evaluate({})
    assert decision.is_allow
    assert warnings == []


def test_engine_first_deny_short_circuits() -> None:
    """First DENY stops evaluation; remaining rules NOT called."""
    called = []

    def tracking_rule(_req: dict) -> Decision:
        called.append("ran")
        return Decision.allow()

    engine = PolicyEngine([_deny_rule, tracking_rule])
    decision, _ = engine.evaluate({})
    assert decision.is_deny
    assert called == []  # Second rule NOT called


def test_engine_warns_collected_when_no_deny() -> None:
    engine = PolicyEngine([_allow_rule, _warn_rule, _warn_rule, _allow_rule])
    decision, warnings = engine.evaluate({})
    assert decision.is_allow
    assert len(warnings) == 2


def test_engine_warns_lost_on_deny() -> None:
    """Warns collected before deny are returned, but final decision is deny."""
    engine = PolicyEngine([_warn_rule, _deny_rule, _allow_rule])
    decision, warnings = engine.evaluate({})
    assert decision.is_deny
    assert len(warnings) == 1  # Warn before deny preserved
