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


def span_to_canonical_dict(span: ReadableSpan) -> dict[str, Any]:
    """Extract span fields relevant pentru chain into a deterministic dict.

    Includes: span name, kind, attributes (with JSON attrs parsed recursively),
    status, start/end time. Excludes: trace_id, span_id (used as identifiers,
    NOT part of canonical body - chain entry has them separately).

    Returns:
        Dict ready for JCS canonicalization.
    """
    attrs = dict(span.attributes or {})
    parsed_attrs: dict[str, Any] = {
        key: _parse_if_json_attr(key, value)
        for key, value in attrs.items()
    }
    # Timestamp-urile OTel sunt nanoseconds since epoch (~1.7 * 10^18) — depășesc
    # JCS safe integer range (2^53 - 1). Stocate ca string pentru lossless canonicalization.
    return {
        "name": span.name,
        "kind": span.kind.name if span.kind else None,
        "attributes": parsed_attrs,
        "status_code": span.status.status_code.name if span.status else None,
        "status_description": span.status.description if span.status else None,
        "start_time_ns": str(span.start_time) if span.start_time is not None else None,
        "end_time_ns": str(span.end_time) if span.end_time is not None else None,
    }


def canonicalize(data: dict[str, Any]) -> bytes:
    """Apply JCS RFC 8785 canonicalization to dict.

    Returns:
        Canonical UTF-8 bytes ready for hashing.
    """
    return rfc8785.dumps(data)
