"""Tests pentru canonical.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from bijotel.processors.canonical import (
    _parse_if_json_attr,
    canonicalize,
    span_to_canonical_dict,
)


def test_parse_json_attr_parses_valid_json() -> None:
    """JSON-stringified value e parse-uit corect."""
    raw = '[{"role": "user", "parts": [{"type": "text", "content": "hi"}]}]'
    result = _parse_if_json_attr("gen_ai.input.messages", raw)
    assert isinstance(result, list)
    assert result[0]["role"] == "user"


def test_parse_json_attr_keeps_invalid_json_as_string() -> None:
    """Invalid JSON e păstrat ca string opac (NU crash)."""
    raw = "not valid {{{"
    result = _parse_if_json_attr("gen_ai.input.messages", raw)
    assert result == raw


def test_parse_json_attr_skips_non_json_keys() -> None:
    """Atribute care NU sunt în JSON_STRINGIFIED_ATTRS sunt păstrate raw."""
    result = _parse_if_json_attr("gen_ai.request.model", "claude-haiku-4-5")
    assert result == "claude-haiku-4-5"


def test_canonicalize_deterministic() -> None:
    """Două dicts semantic identice produc bytes identice."""
    a = {"b": 2, "a": 1, "c": [3, 1, 2]}
    b = {"a": 1, "b": 2, "c": [3, 1, 2]}
    assert canonicalize(a) == canonicalize(b)


def test_canonicalize_different_for_different_dicts() -> None:
    """Dicts diferite produc bytes diferite."""
    a = {"a": 1}
    b = {"a": 2}
    assert canonicalize(a) != canonicalize(b)


def test_jcs_canonicalization_idempotent_on_nested_json() -> None:
    """Parse + canonicalize peste nested JSON e idempotent.

    Două calls semantic identice cu order intern diferit -> același hash.
    """
    raw_a = '[{"role": "user", "parts": []}]'
    raw_b = '[{"parts": [], "role": "user"}]'

    parsed_a = json.loads(raw_a)
    parsed_b = json.loads(raw_b)

    assert canonicalize(parsed_a) == canonicalize(parsed_b)


def test_span_to_canonical_dict_extracts_fields() -> None:
    """span_to_canonical_dict extrage toate câmpurile relevante."""
    span = MagicMock()
    span.name = "anthropic.chat"
    span.kind.name = "CLIENT"
    span.attributes = {
        "gen_ai.request.model": "claude-haiku-4-5",
        "gen_ai.input.messages": '[{"role":"user","parts":[]}]',
    }
    span.status.status_code.name = "OK"
    span.status.description = None
    span.start_time = 1000
    span.end_time = 2000

    result = span_to_canonical_dict(span)
    assert result["name"] == "anthropic.chat"
    assert result["kind"] == "CLIENT"
    assert isinstance(result["attributes"]["gen_ai.input.messages"], list)
    assert result["attributes"]["gen_ai.request.model"] == "claude-haiku-4-5"
    assert result["start_time_ns"] == "1000"  # ns timestamps stored as string (JCS limit)
