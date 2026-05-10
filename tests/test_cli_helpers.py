"""Tests for private CLI helpers (_parse_canonical_body, _detect_status).

These cover edge-case branches missed by integration tests via cli_main.
"""

from __future__ import annotations

from bijotel.cli.commands import _detect_status, _parse_canonical_body


def test_parse_canonical_body_invalid_json_returns_empty() -> None:
    """Garbage JSON bytes → empty dict (graceful)."""
    assert _parse_canonical_body(b"{not valid json") == {}


def test_parse_canonical_body_invalid_utf8_returns_empty() -> None:
    """Non-UTF-8 bytes → empty dict (graceful)."""
    assert _parse_canonical_body(b"\xff\xfe\x00") == {}


def test_parse_canonical_body_valid() -> None:
    """Valid JSON-encoded UTF-8 → parsed dict."""
    body = b'{"attributes": {"x": 1}}'
    assert _parse_canonical_body(body) == {"attributes": {"x": 1}}


def test_detect_status_warn_branch() -> None:
    """Span with bijotel.policy.warning attr → 'WARN'."""
    body = {"attributes": {"bijotel.policy.warning": "rule_x"}}
    assert _detect_status(body) == "WARN"


def test_detect_status_blocked_branch() -> None:
    """Span with bijotel.blocked=True → 'BLOCKED'."""
    body = {"attributes": {"bijotel.blocked": True}}
    assert _detect_status(body) == "BLOCKED"


def test_detect_status_allowed_default() -> None:
    """Plain span (no policy attrs) → 'ALLOWED'."""
    body = {"attributes": {"gen_ai.request.model": "x"}}
    assert _detect_status(body) == "ALLOWED"


def test_detect_status_no_attributes() -> None:
    """Missing attributes key → 'ALLOWED' (default)."""
    assert _detect_status({}) == "ALLOWED"
