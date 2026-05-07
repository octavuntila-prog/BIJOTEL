"""Tests pentru Decision class."""

from __future__ import annotations

import pytest

from bijotel.policy import Decision, PolicyDeniedError


def test_allow() -> None:
    d = Decision.allow()
    assert d.is_allow
    assert not d.is_warn
    assert not d.is_deny


def test_warn() -> None:
    d = Decision.warn(rule="test_rule", reason="test reason")
    assert d.is_warn
    assert d.rule == "test_rule"
    assert d.reason == "test reason"


def test_deny() -> None:
    d = Decision.deny(rule="r", reason="x")
    assert d.is_deny
    assert d.redact_input is False  # default


def test_deny_with_redact_input() -> None:
    d = Decision.deny(rule="pii", reason="found", redact_input=True)
    assert d.is_deny
    assert d.redact_input is True


def test_decision_immutable() -> None:
    """Decision is frozen dataclass."""
    d = Decision.allow()
    with pytest.raises(Exception):  # noqa: B017,PT011 - FrozenInstanceError
        d.state = "DENY"  # type: ignore[misc]


def test_policy_denied_error() -> None:
    e = PolicyDeniedError(rule="r1", reason="r2")
    assert e.rule == "r1"
    assert e.reason == "r2"
    assert "r1" in str(e)
    assert "r2" in str(e)
