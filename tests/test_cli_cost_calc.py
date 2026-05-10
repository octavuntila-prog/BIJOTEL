"""Tests for _calc_cost helper (v0.2.1 bugfix).

Empirical bug discovered post-v0.2.0:
  - Sonnet 4 (claude-sonnet-4-20250514) returned "?" because not in price table
  - Tiny Haiku calls returned "$0.0000" (indistinguishable from blocked spans)

This file pins all 5 _calc_cost return-value branches.
"""

from __future__ import annotations

from bijotel.cli.commands import _calc_cost


def test_calc_cost_dash_when_tokens_missing() -> None:
    """No usage tokens → '-' (cannot compute)."""
    body = {"attributes": {"gen_ai.request.model": "claude-haiku-4-5"}}
    assert _calc_cost(body) == "-"

    # Half-missing also dash
    body2 = {
        "attributes": {
            "gen_ai.request.model": "claude-haiku-4-5",
            "gen_ai.usage.input_tokens": 10,
        }
    }
    assert _calc_cost(body2) == "-"


def test_calc_cost_unknown_model_includes_model_name() -> None:
    """Unknown model → '?<model>' fragment, NOT bare '?'.

    Pre-v0.2.1 returned bare '?'. Now includes model name for actionable
    feedback (user can update price table).
    """
    body = {
        "attributes": {
            "gen_ai.request.model": "claude-future-99-202999",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 5,
        }
    }
    result = _calc_cost(body)
    assert result.startswith("?")
    assert "claude-future-99" in result


def test_calc_cost_zero_for_actually_zero_tokens() -> None:
    """Zero tokens (e.g. blocked span no real call) → '$0.0000' literal."""
    body = {
        "attributes": {
            "gen_ai.request.model": "claude-haiku-4-5",
            "gen_ai.usage.input_tokens": 0,
            "gen_ai.usage.output_tokens": 0,
        }
    }
    assert _calc_cost(body) == "$0.0000"


def test_calc_cost_lt_threshold_for_tiny_real_calls() -> None:
    """Tiny real call (rounds to 0 at 4 decimals) → '<$0.0001'.

    Pre-v0.2.1 returned '$0.0000' which was ambiguous with blocked spans.
    14 input + 4 output Haiku tokens = $0.0000272 → was '$0.0000', now '<$0.0001'.
    """
    body = {
        "attributes": {
            "gen_ai.request.model": "claude-haiku-4-5-20251001",
            "gen_ai.usage.input_tokens": 14,
            "gen_ai.usage.output_tokens": 4,
        }
    }
    assert _calc_cost(body) == "<$0.0001"


def test_calc_cost_normal_format_for_typical_calls() -> None:
    """Typical Sonnet call (~$0.01-0.05) → standard $X.XXXX format."""
    body = {
        "attributes": {
            "gen_ai.request.model": "claude-sonnet-4-20250514",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 500,
        }
    }
    # 1000 * 0.0030 / 1000 + 500 * 0.0150 / 1000 = 0.003 + 0.0075 = 0.0105
    assert _calc_cost(body) == "$0.0105"


def test_calc_cost_sonnet_4_in_price_table_now() -> None:
    """v0.2.1 fix: claude-sonnet-4-20250514 NOW recognized.

    Pre-v0.2.1: this model returned '?' because price table only had
    claude-sonnet-4-6 (newer hypothetical). Production GENA was using
    claude-sonnet-4-20250514, leading to all production cost = '?' in chain.
    """
    body = {
        "attributes": {
            "gen_ai.request.model": "claude-sonnet-4-20250514",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 200,
        }
    }
    result = _calc_cost(body)
    assert result.startswith("$")
    assert "?" not in result


def test_calc_cost_haiku_models_unchanged() -> None:
    """Haiku models continue to work (regression check)."""
    body = {
        "attributes": {
            "gen_ai.request.model": "claude-haiku-4-5",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 1000,
        }
    }
    # 1000 * 0.0008 / 1000 + 1000 * 0.0040 / 1000 = 0.0008 + 0.0040 = 0.0048
    assert _calc_cost(body) == "$0.0048"


def test_calc_cost_empty_attributes_returns_dash() -> None:
    """No attributes at all → '-' (graceful)."""
    assert _calc_cost({}) == "-"
    assert _calc_cost({"attributes": {}}) == "-"


def test_calc_cost_empty_model_returns_unknown_marker() -> None:
    """Empty model string + tokens present → '?(none)' or similar."""
    body = {
        "attributes": {
            "gen_ai.request.model": "",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 5,
        }
    }
    result = _calc_cost(body)
    assert result.startswith("?")
