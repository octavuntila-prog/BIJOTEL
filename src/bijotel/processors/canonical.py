"""Canonicalization: span -> deterministic JSON bytes via JCS (RFC 8785)."""

from __future__ import annotations

import json
from typing import Any

import rfc8785
from opentelemetry.sdk.trace import ReadableSpan

# Atribute care conțin JSON serialized intern și trebuie parse-uite recursiv
# înainte de canonicalization (vezi F1 schema discovery).
JSON_STRINGIFIED_ATTRS = frozenset({
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.definitions",
})

# Top-level keys excluse pentru semantic dedup (CAS).
# Acestea variază per call indiferent de input identic.
SEMANTIC_EXCLUDE_TOP_LEVEL = frozenset({
    "start_time_ns",
    "end_time_ns",
    "status_code",
    "status_description",
})

# Attribute keys (din span.attributes) excluse pentru semantic dedup.
# Hardcoded explicit — fiecare addition e audit conștient input vs output.
SEMANTIC_EXCLUDE_ATTRS = frozenset({
    # Output messages (variază pentru modele non-deterministe)
    "gen_ai.output.messages",
    # Response metadata (per-call, NU "what was asked")
    "gen_ai.response.id",
    "gen_ai.response.model",
    "gen_ai.response.finish_reasons",
    # Usage (depinde de output)
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
})


def _parse_if_json_attr(key: str, value: Any) -> Any:
    """Parse value if key is in JSON_STRINGIFIED_ATTRS and value is a string.

    Returns parsed value on success, original value on parse failure.
    """
    if key not in JSON_STRINGIFIED_ATTRS:
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value  # Non-JSON content - keep as opaque string


def span_to_dict(
    span: ReadableSpan,
    *,
    exclude_top_level: frozenset[str] = frozenset(),
    exclude_attrs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Extract span fields into deterministic dict, with optional exclusions.

    Args:
        span: ReadableSpan to extract from.
        exclude_top_level: Top-level keys to omit (e.g. "start_time_ns").
        exclude_attrs: Span attribute keys to omit (e.g. "gen_ai.output.messages").

    Returns:
        Dict ready for JCS canonicalization.
    """
    attrs = dict(span.attributes or {})
    parsed_attrs: dict[str, Any] = {
        key: _parse_if_json_attr(key, value)
        for key, value in attrs.items()
        if key not in exclude_attrs
    }
    # Timestamp-urile OTel sunt nanoseconds since epoch (~1.7 * 10^18) — depășesc
    # JCS safe integer range (2^53 - 1). Stocate ca string pentru lossless canonicalization.
    full = {
        "name": span.name,
        "kind": span.kind.name if span.kind else None,
        "attributes": parsed_attrs,
        "status_code": span.status.status_code.name if span.status else None,
        "status_description": span.status.description if span.status else None,
        "start_time_ns": str(span.start_time) if span.start_time is not None else None,
        "end_time_ns": str(span.end_time) if span.end_time is not None else None,
    }
    return {k: v for k, v in full.items() if k not in exclude_top_level}


def span_to_canonical_dict(span: ReadableSpan) -> dict[str, Any]:
    """Extract span fields with NO exclusions (full canonical body).

    Used by HmacChainSpanProcessor for tamper-evident chain.
    Two calls with identical input but different output produce different
    canonical dicts (because output, timestamps, IDs are included).
    """
    return span_to_dict(span)


def span_to_semantic_dict(span: ReadableSpan) -> dict[str, Any]:
    """Extract span fields excluding output, usage, response, IDs, timestamps.

    Used by CasSpanProcessor for input-only content-addressable dedup.
    Two calls with identical input produce identical semantic dict, regardless
    of output, timing, or response IDs.
    """
    return span_to_dict(
        span,
        exclude_top_level=SEMANTIC_EXCLUDE_TOP_LEVEL,
        exclude_attrs=SEMANTIC_EXCLUDE_ATTRS,
    )


def canonicalize(data: dict[str, Any]) -> bytes:
    """Apply JCS RFC 8785 canonicalization to dict.

    Returns:
        Canonical UTF-8 bytes ready for hashing.
    """
    return rfc8785.dumps(data)
