"""Tests for F16 / Bijuteria #10 completion — compliance policy rules."""

from __future__ import annotations

import pytest

from bijotel.policy import (
    PolicyEngine,
    model_version_pin,
    output_length_limit,
    pii_detection,
)

# === pii_detection ===


def test_pii_email_detected() -> None:
    rule = pii_detection(mode="warn")
    d = rule({"messages": [{"role": "user", "content": "Email me at john@example.com"}]})
    assert d.is_warn
    assert "email" in (d.reason or "")


def test_pii_phone_detected() -> None:
    rule = pii_detection(mode="warn")
    d = rule({"messages": [{"role": "user", "content": "Call (555) 123-4567 now"}]})
    assert d.is_warn
    assert "phone" in (d.reason or "")


def test_pii_ssn_detected() -> None:
    rule = pii_detection(mode="deny")
    d = rule({"messages": [{"role": "user", "content": "SSN is 123-45-6789"}]})
    assert d.is_deny
    assert "ssn" in (d.reason or "")


def test_pii_clean_prompt_allows() -> None:
    rule = pii_detection(mode="warn")
    d = rule({"messages": [{"role": "user", "content": "Tell me about cats"}]})
    assert d.is_allow


def test_pii_custom_patterns() -> None:
    rule = pii_detection(patterns={"company_id": r"COMP-\d{4}"}, mode="warn")
    d = rule({"messages": [{"role": "user", "content": "ref COMP-1234"}]})
    assert d.is_warn
    assert "company_id" in (d.reason or "")


def test_pii_invalid_mode_raises() -> None:
    with pytest.raises(ValueError, match="mode must be"):
        pii_detection(mode="block")


# === output_length_limit ===


def test_output_length_under_limit_allows() -> None:
    rule = output_length_limit(max_tokens=4096)
    d = rule({"max_tokens": 1000})
    assert d.is_allow


def test_output_length_over_limit_warns() -> None:
    rule = output_length_limit(max_tokens=4096, mode="warn")
    d = rule({"max_tokens": 8000})
    assert d.is_warn
    assert "8000" in (d.reason or "")
    assert "4096" in (d.reason or "")


def test_output_length_over_limit_denies() -> None:
    rule = output_length_limit(max_tokens=2000, mode="deny")
    d = rule({"max_tokens": 5000})
    assert d.is_deny


def test_output_length_missing_field_allows() -> None:
    """If max_tokens not in request, allow (nothing to enforce)."""
    rule = output_length_limit(max_tokens=4096)
    d = rule({})
    assert d.is_allow


def test_output_length_invalid_args() -> None:
    with pytest.raises(ValueError, match="max_tokens must be >= 1"):
        output_length_limit(max_tokens=0)


# === model_version_pin ===


def test_model_version_pin_exact_match_allows() -> None:
    rule = model_version_pin(allowed_versions=["claude-sonnet-4-20250514"])
    d = rule({"model": "claude-sonnet-4-20250514"})
    assert d.is_allow


def test_model_version_pin_unpinned_denies() -> None:
    rule = model_version_pin(allowed_versions=["claude-sonnet-4-20250514"])
    d = rule({"model": "claude-sonnet-4"})  # family alias, not exact version
    assert d.is_deny
    assert "silent-upgrade" in (d.reason or "")


def test_model_version_pin_empty_allowlist_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        model_version_pin(allowed_versions=[])


def test_combined_pii_plus_length_plus_pin() -> None:
    """All 3 new rules + existing F11 compose in PolicyEngine."""
    engine = PolicyEngine(
        [
            pii_detection(mode="warn"),
            output_length_limit(max_tokens=4096, mode="warn"),
            model_version_pin(allowed_versions=["claude-haiku-4-5-20251001"], mode="warn"),
        ]
    )
    # All 3 violated: PII email + over-limit + non-pinned model
    decision, warnings = engine.evaluate(
        {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "Reach me at x@y.com"}],
            "max_tokens": 9000,
        }
    )
    rules = {w.rule for w in warnings}
    assert rules == {"pii_detection", "output_length_limit", "model_version_pin"}
