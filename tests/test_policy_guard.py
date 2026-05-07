"""Tests pentru guard() decorator + synthetic span emission."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.policy import Decision, PolicyDeniedError, guard
from bijotel.processors import HmacChainSpanProcessor

SECRET = b"x" * 32


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "guard.db"


@pytest.fixture
def provider_with_chain(db_path: Path) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    )
    trace.set_tracer_provider(provider)
    return provider


def _make_rule(decision: Decision) -> Callable[[dict], Decision]:
    def rule(_request: dict) -> Decision:
        return decision

    return rule


def test_guard_allow_calls_fn(provider_with_chain: TracerProvider) -> None:
    fn = MagicMock(return_value="result")
    guarded = guard(fn, policy=[_make_rule(Decision.allow())])
    result = guarded(model="claude-haiku-4-5", messages=[], max_tokens=10)
    assert result == "result"
    fn.assert_called_once()


def test_guard_deny_raises_and_skips_fn(
    provider_with_chain: TracerProvider,
) -> None:
    fn = MagicMock()
    guarded = guard(
        fn,
        policy=[_make_rule(Decision.deny(rule="test", reason="x"))],
    )
    with pytest.raises(PolicyDeniedError, match="test"):
        guarded(model="claude-haiku-4-5", messages=[], max_tokens=10)
    fn.assert_not_called()


def test_guard_warn_emits_span_and_calls_fn(
    provider_with_chain: TracerProvider,
) -> None:
    fn = MagicMock(return_value="ok")
    guarded = guard(
        fn,
        policy=[_make_rule(Decision.warn(rule="test", reason="x"))],
    )
    result = guarded(model="claude-haiku-4-5", messages=[], max_tokens=10)
    assert result == "ok"
    fn.assert_called_once()


def test_guard_deny_emits_synthetic_span_in_chain(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    fn = MagicMock()
    guarded = guard(
        fn,
        policy=[_make_rule(Decision.deny(rule="test", reason="reason"))],
    )
    with pytest.raises(PolicyDeniedError):
        guarded(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "test"}],
            max_tokens=10,
        )
    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT canonical_body FROM chain WHERE span_name = 'bijotel.policy.gate'"
        ).fetchall()
        assert len(rows) == 1
        body = rows[0][0]
        assert b'"bijotel.blocked":true' in body
        assert b'"bijotel.policy.rule":"test"' in body


def test_guard_redact_input_hides_messages(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """redact_input=True -> messages stocat ca sha256 hash, NU plain text."""
    fn = MagicMock()
    guarded = guard(
        fn,
        policy=[
            _make_rule(
                Decision.deny(rule="pii", reason="found", redact_input=True)
            )
        ],
    )
    with pytest.raises(PolicyDeniedError):
        guarded(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": "SECRET_PII_DATA"}],
            max_tokens=10,
        )
    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute(
            "SELECT canonical_body FROM chain WHERE span_name = 'bijotel.policy.gate'"
        ).fetchone()[0]
        # Plain text NU e in chain
        assert b"SECRET_PII_DATA" not in body
        # Hash IS in chain
        assert b"sha256:" in body
        # Flag e setat
        assert b'"bijotel.policy.input_redacted":true' in body
