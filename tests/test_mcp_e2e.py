"""End-to-end MCP sealing test (v2.13.3) — NOT mocked.

Stands up a *real* in-memory MCP server + client (the mcp SDK's in-memory
transport), instruments via :class:`MCPInstrumentor`, makes a real
``call_tool``, and asserts the invocation is sealed into ``chain.db`` by the
**default** :class:`HmacChainSpanProcessor`.

This is the guard that was missing: the original 18 MCP unit tests mocked
``ClientSession`` and never verified a real sealed invocation — which hid that
the default span filter (``gen_ai.*`` only) dropped MCP spans. Fixed in v2.13.3
(filter now also keeps ``bijotel.mcp.*``); this test proves the full path.

Requires the ``mcp`` SDK (``bijotel[mcp]``); the whole module skips otherwise.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("mcp")  # skip the module entirely if the MCP SDK is absent


@pytest.mark.asyncio
async def test_mcp_call_tool_sealed_by_default_processor(tmp_path: Path) -> None:
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.memory import (
        create_connected_server_and_client_session as connect,
    )
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    from bijotel.mcp import MCPInstrumentor
    from bijotel.processors import HmacChainSpanProcessor

    db = tmp_path / "chain.db"
    provider = TracerProvider()
    # DEFAULT processor (no custom filter) — proves MCP seals out-of-the-box.
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db, secret_key=b"x" * 32)
    )
    trace.set_tracer_provider(provider)

    server = FastMCP("bijotel-e2e-test")

    @server.tool()
    def echo(text: str) -> str:
        return text

    inst = MCPInstrumentor()
    inst.instrument()
    try:
        async with connect(server) as session:
            await session.initialize()
            result = await session.call_tool("echo", {"text": "hello-bijotel"})
            assert result is not None
    finally:
        inst.uninstrument()
        provider.force_flush()

    rows = sqlite3.connect(db).execute(
        "SELECT seq, canonical_body FROM chain ORDER BY seq"
    ).fetchall()
    bodies = [
        (b if isinstance(b, str) else (b.decode() if b else "")) for _, b in rows
    ]
    mcp_bodies = [b for b in bodies if "bijotel.mcp.tool_name" in b]
    assert mcp_bodies, (
        f"MCP invocation was NOT sealed (default filter dropped it?). "
        f"chain has {len(rows)} rows."
    )
    sealed = json.loads(mcp_bodies[-1])
    flat = json.dumps(sealed)
    assert "mcp.tool.echo" in flat, "span name not in sealed body"
    assert '"echo"' in flat, "tool_name 'echo' not in sealed attributes"
