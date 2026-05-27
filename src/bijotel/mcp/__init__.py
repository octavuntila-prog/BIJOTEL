"""BIJOTEL MCP invocation sealing — v2.12.0.

Seals MCP (Model Context Protocol) tool invocations into the same HMAC
chain alongside LLM calls. Each ``ClientSession.call_tool(name, args)``
invocation becomes a span carrying ``bijotel.mcp.*`` attributes, which
the existing ``HmacChainSpanProcessor`` then seals into ``chain.db``.

Usage::

    from bijotel.mcp import MCPInstrumentor

    MCPInstrumentor().instrument()
    # All subsequent ClientSession.call_tool(...) calls are sealed.

See ``docs/design/bijotel-mcp.md`` for the full protocol design.
"""

from __future__ import annotations

from bijotel.mcp.attributes import (
    MCP_ATTRS,
    mcp_invocation_context,
)
from bijotel.mcp.instrumentor import MCPInstrumentor

__all__ = [
    "MCPInstrumentor",
    "MCP_ATTRS",
    "mcp_invocation_context",
]
