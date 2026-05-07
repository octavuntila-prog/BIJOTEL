"""Tests pentru canonical.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from bijotel.processors.canonical import (
    _parse_if_json_attr,
    canonicalize,
    span_to_canonical_dict,
    span_to_dict,
    span_to_semantic_dict,
)


def _build_span(
    *,
    name: str = "anthropic.chat",
    kind_name: str = "CLIENT",
    attributes: dict | None = None,
    status_code_name: str = "OK",
    status_description: str | None = None,
    start_time: int = 1000,
    end_time: int = 2000,
) -> MagicMock:
    """Helper: build a MagicMock span with sane defaults."""
    span = MagicMock()
    span.name = name
    span.kind.name = kind_name
    span.attributes = attributes or {}
    span.status.status_code.name = status_code_name
    span.status.description = status_description
    span.start_time = start_time
    span.end_time = end_time
    return span


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
    span = _build_span(
        attributes={
            "gen_ai.request.model": "claude-haiku-4-5",
            "gen_ai.input.messages": '[{"role":"user","parts":[]}]',
        }
    )

    result = span_to_canonical_dict(span)
    assert result["name"] == "anthropic.chat"
    assert result["kind"] == "CLIENT"
    assert isinstance(result["attributes"]["gen_ai.input.messages"], list)
    assert result["attributes"]["gen_ai.request.model"] == "claude-haiku-4-5"
    assert result["start_time_ns"] == "1000"  # ns timestamps stored as string (JCS limit)


def test_span_to_dict_default_no_exclusions() -> None:
    """span_to_dict() fără args = include toate fields."""
    span = _build_span(
        attributes={
            "gen_ai.request.model": "claude",
            "gen_ai.output.messages": "[]",
        }
    )
    result = span_to_dict(span)
    assert "start_time_ns" in result
    assert "status_code" in result
    assert "gen_ai.output.messages" in result["attributes"]


def test_span_to_dict_excludes_top_level() -> None:
    """exclude_top_level omits specified keys."""
    span = _build_span(
        name="x",
        attributes={"gen_ai.request.model": "claude"},
    )
    result = span_to_dict(
        span, exclude_top_level=frozenset({"start_time_ns", "end_time_ns"})
    )
    assert "start_time_ns" not in result
    assert "end_time_ns" not in result
    assert "name" in result


def test_span_to_dict_excludes_attrs() -> None:
    """exclude_attrs omits specified span.attributes keys."""
    span = _build_span(
        name="x",
        attributes={
            "gen_ai.request.model": "claude",
            "gen_ai.output.messages": "[]",
        },
    )
    result = span_to_dict(
        span, exclude_attrs=frozenset({"gen_ai.output.messages"})
    )
    assert "gen_ai.output.messages" not in result["attributes"]
    assert "gen_ai.request.model" in result["attributes"]


def test_span_to_semantic_dict_excludes_output_and_timestamps() -> None:
    """span_to_semantic_dict() excludes output, usage, response, timestamps."""
    span = _build_span(
        attributes={
            "gen_ai.request.model": "claude",
            "gen_ai.input.messages": '[{"role":"user","parts":[]}]',
            "gen_ai.output.messages": '[{"role":"assistant","parts":[]}]',
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.response.id": "msg_abc",
        }
    )

    result = span_to_semantic_dict(span)
    assert "gen_ai.input.messages" in result["attributes"]
    assert "gen_ai.request.model" in result["attributes"]
    assert "gen_ai.output.messages" not in result["attributes"]
    assert "gen_ai.usage.input_tokens" not in result["attributes"]
    assert "gen_ai.response.id" not in result["attributes"]
    assert "start_time_ns" not in result
    assert "end_time_ns" not in result


def test_semantic_dict_identical_for_calls_with_same_input() -> None:
    """Două spans cu același input dar output diferit -> semantic_dict identic."""
    common_input = '[{"role":"user","parts":[{"type":"text","content":"hi"}]}]'

    span_a = _build_span(
        attributes={
            "gen_ai.input.messages": common_input,
            "gen_ai.request.model": "claude",
            "gen_ai.output.messages": (
                '[{"role":"assistant","parts":[{"type":"text","content":"OUTPUT_A"}]}]'
            ),
            "gen_ai.usage.output_tokens": 10,
        },
        start_time=1000,
        end_time=2000,
    )

    span_b = _build_span(
        attributes={
            "gen_ai.input.messages": common_input,
            "gen_ai.request.model": "claude",
            "gen_ai.output.messages": (
                '[{"role":"assistant","parts":[{"type":"text","content":"OUTPUT_B_DIFF"}]}]'
            ),
            "gen_ai.usage.output_tokens": 25,
        },
        start_time=5000,
        end_time=9999,
    )

    sem_a = span_to_semantic_dict(span_a)
    sem_b = span_to_semantic_dict(span_b)

    assert canonicalize(sem_a) == canonicalize(sem_b), (
        "Calls cu input identic dar output diferit trebuie să aibă semantic dict identic"
    )
