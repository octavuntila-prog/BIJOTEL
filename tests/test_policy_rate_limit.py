"""Tests for rate_limit_calls_per_minute rule (F8)."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from bijotel.policy import rate_limit_calls_per_minute
from bijotel.policy.decision import Decision


def test_rate_limit_allows_under_threshold(tmp_path: Path) -> None:
    """N-1 calls when limit is N -> all allowed."""
    db = tmp_path / "rl.db"
    rule = rate_limit_calls_per_minute(max_calls=5, db_path=db)

    request = {"model": "claude-haiku-4-5-20251001", "messages": []}
    for _ in range(4):
        decision = rule(request)
        assert decision.is_allow is True
        assert decision.rule == ""  # Decision.allow() has rule=""


def test_rate_limit_denies_at_threshold(tmp_path: Path) -> None:
    """Nth call when limit is N -> denied (already N-1 in window)."""
    db = tmp_path / "rl.db"
    rule = rate_limit_calls_per_minute(max_calls=3, db_path=db)

    request = {"model": "claude-haiku-4-5-20251001", "messages": []}
    # Fill window to exactly max_calls
    for _ in range(3):
        decision = rule(request)
        assert decision.is_allow is True

    # Next call should deny
    decision = rule(request)
    assert decision.is_deny is True
    assert decision.rule == "rate_limit_calls_per_minute"
    assert "Rate limit exceeded" in (decision.reason or "")


def test_rate_limit_warn_mode(tmp_path: Path) -> None:
    """warn mode allows but tags decision."""
    db = tmp_path / "rl.db"
    rule = rate_limit_calls_per_minute(max_calls=2, db_path=db, mode="warn")

    request = {"model": "claude-haiku-4-5-20251001", "messages": []}
    rule(request)
    rule(request)
    decision = rule(request)
    # In warn mode: decision is_warn=True (engine treats warn as "audit but proceed")
    assert decision.is_warn is True
    assert decision.rule == "rate_limit_calls_per_minute"


def test_rate_limit_invalid_mode(tmp_path: Path) -> None:
    db = tmp_path / "rl.db"
    with pytest.raises(ValueError, match="must be 'deny' or 'warn'"):
        rate_limit_calls_per_minute(max_calls=10, db_path=db, mode="bogus")


def test_rate_limit_invalid_max_calls(tmp_path: Path) -> None:
    db = tmp_path / "rl.db"
    with pytest.raises(ValueError, match="max_calls must be >= 1"):
        rate_limit_calls_per_minute(max_calls=0, db_path=db)


def test_rate_limit_pruning_old_entries(tmp_path: Path) -> None:
    """Old timestamps (>60s) are pruned at evaluation time."""
    db = tmp_path / "rl.db"
    # Pre-seed table with stale entries (> 60s old)
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS policy_rate_limit_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ns INTEGER NOT NULL
            )
        """)
        old_ns = time.time_ns() - 120 * 1_000_000_000  # 120 sec ago
        for _ in range(10):
            conn.execute(
                "INSERT INTO policy_rate_limit_state (timestamp_ns) VALUES (?)",
                (old_ns,),
            )
        conn.commit()

    rule = rate_limit_calls_per_minute(max_calls=3, db_path=db)

    # All 10 stale entries should be pruned -> first call allowed
    request = {"model": "claude-haiku-4-5-20251001", "messages": []}
    decision = rule(request)
    assert decision.is_allow is True

    # Verify pruning happened
    with sqlite3.connect(db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM policy_rate_limit_state"
        ).fetchone()[0]
        assert count == 1  # Only the current call's entry


def test_rate_limit_persists_across_rule_instances(tmp_path: Path) -> None:
    """State persists in SQLite — new rule instance sees previous state."""
    db = tmp_path / "rl.db"
    request = {"model": "claude-haiku-4-5-20251001", "messages": []}

    rule_a = rate_limit_calls_per_minute(max_calls=3, db_path=db)
    rule_a(request)
    rule_a(request)

    # New rule instance, same db
    rule_b = rate_limit_calls_per_minute(max_calls=3, db_path=db)
    rule_b(request)  # 3rd call — should still allow (count was 2 before)

    decision = rule_b(request)  # 4th call — should deny
    assert decision.is_deny is True
    assert decision.rule == "rate_limit_calls_per_minute"


def test_rate_limit_independent_databases(tmp_path: Path) -> None:
    """Different db_path = independent rate limit state."""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"

    rule_a = rate_limit_calls_per_minute(max_calls=2, db_path=db_a)
    rule_b = rate_limit_calls_per_minute(max_calls=2, db_path=db_b)

    request = {"model": "claude-haiku-4-5-20251001", "messages": []}

    # Fill db_a to limit
    rule_a(request)
    rule_a(request)
    decision_a = rule_a(request)
    assert decision_a.is_deny is True

    # db_b is independent — should still allow
    decision_b = rule_b(request)
    assert decision_b.is_allow is True


def test_rate_limit_returns_decision_type(tmp_path: Path) -> None:
    """Sanity: rule returns Decision instances."""
    db = tmp_path / "rl.db"
    rule = rate_limit_calls_per_minute(max_calls=10, db_path=db)
    decision = rule({"model": "x", "messages": []})
    assert isinstance(decision, Decision)
