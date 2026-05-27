"""MCP span attribute vocabulary + helpers (v2.12.0).

Authoritative list of ``bijotel.mcp.*`` attributes BIJOTEL captures per
MCP tool invocation. Hashes (SHA-256 hex) are used for inputs and
outputs rather than raw bytes — hashes preserve forensic value (verify
against a known-good blob later) without exposing potentially-sensitive
content (file paths, credentials, prompts) in the chain.

See ``docs/design/bijotel-mcp.md`` for the full protocol design.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Authoritative attribute list. Type-only documentation; OTel sets
# attribute values directly via ``span.set_attribute(key, value)``.
MCP_ATTRS: dict[str, type] = {
    "bijotel.mcp.server_name": str,
    "bijotel.mcp.server_version": str,
    "bijotel.mcp.tool_name": str,
    "bijotel.mcp.tool_input_hash": str,
    "bijotel.mcp.tool_output_hash": str,
    "bijotel.mcp.caller": str,
    "bijotel.mcp.duration_ms": float,
    "bijotel.mcp.status": str,
    "bijotel.mcp.error_type": str,
    "bijotel.mcp.transport": str,
}


def _canonicalize(value: Any) -> str:
    """Canonicalize a value to a deterministic string for hashing.

    - ``dict`` and ``list`` go through ``json.dumps(sort_keys=True,
      ensure_ascii=False)`` so the same logical content always hashes
      identically.
    - ``None`` becomes the empty string (so output_hash for failed calls
      is deterministic).
    - Anything else goes through ``str()``.

    Determinism guarantees: the same input produces the same hash on any
    machine, any Python version (with stable JSON ordering).
    """
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def _hash(value: Any) -> str:
    """SHA-256 hex of the canonicalized value. Empty string for ``None``."""
    canonical = _canonicalize(value)
    if not canonical:
        return ""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mcp_invocation_context(
    *,
    server_name: str,
    tool_name: str,
    tool_input: Any,
    tool_output: Any = None,
    server_version: str = "",
    caller: str = "",
    duration_ms: float = 0.0,
    status: str = "success",
    error_type: str = "",
    transport: str = "unknown",
) -> dict[str, Any]:
    """Build the ``bijotel.mcp.*`` attribute dict for one MCP invocation.

    Used by ``MCPInstrumentor`` to populate spans, and exposed publicly so
    callers who want to emit MCP spans manually (e.g. from a non-stdlib
    MCP client) can do so without re-implementing hashing.

    Args:
        server_name: MCP server identity. Required.
        tool_name: Tool invoked. Required.
        tool_input: Arguments passed to the tool. Hashed (not stored raw).
        tool_output: Result returned. Hashed. ``None`` for failed calls.
        server_version: Optional — server version string.
        caller: Optional — who invoked (agent name, user id).
        duration_ms: Optional — invocation duration.
        status: ``"success"`` or ``"error"``.
        error_type: Exception class name when ``status="error"``.
        transport: ``"stdio"`` / ``"sse"`` / ``"streamable-http"`` /
                   ``"unknown"``.

    Returns:
        Flat dict with ``bijotel.mcp.*`` keys, ready for
        ``span.set_attributes(...)``.
    """
    attrs: dict[str, Any] = {
        "bijotel.mcp.server_name": server_name,
        "bijotel.mcp.tool_name": tool_name,
        "bijotel.mcp.tool_input_hash": _hash(tool_input),
        "bijotel.mcp.tool_output_hash": _hash(tool_output),
        "bijotel.mcp.status": status,
        "bijotel.mcp.transport": transport,
        "bijotel.mcp.duration_ms": float(duration_ms),
    }
    # Optional attributes — only set when populated (avoid empty noise).
    if server_version:
        attrs["bijotel.mcp.server_version"] = server_version
    if caller:
        attrs["bijotel.mcp.caller"] = caller
    if error_type:
        attrs["bijotel.mcp.error_type"] = error_type
    return attrs
