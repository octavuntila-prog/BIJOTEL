"""Tests for the bijotel-mcp instrumentor + attribute helpers (v2.12.0).

The instrumentor patches ``mcp.ClientSession.call_tool``, but the tests
avoid depending on a live MCP server by stubbing ``ClientSession``
in-tree (via ``sys.modules`` injection). This keeps the test suite
hermetic — no MCP SDK install required to run them.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import types

import pytest

from bijotel.mcp.attributes import (
    MCP_ATTRS,
    _canonicalize,
    _hash,
    mcp_invocation_context,
)

# ─── 1. Attribute helpers ──────────────────────────────────────────────


def test_canonicalize_dict_is_sorted() -> None:
    """Dict canonical form is sort_keys=True — order-independent."""
    a = _canonicalize({"b": 1, "a": 2})
    b = _canonicalize({"a": 2, "b": 1})
    assert a == b == '{"a": 2, "b": 1}'


def test_canonicalize_none_is_empty_string() -> None:
    assert _canonicalize(None) == ""


def test_canonicalize_list_is_json() -> None:
    assert _canonicalize([1, 2, 3]) == "[1, 2, 3]"


def test_canonicalize_unicode_preserved() -> None:
    # ensure_ascii=False so non-Latin scripts hash by content, not by \u escapes.
    assert _canonicalize({"ro": "ăîâșț"}) == '{"ro": "ăîâșț"}'


def test_hash_is_sha256() -> None:
    h = _hash({"a": 1})
    expected = hashlib.sha256(b'{"a": 1}').hexdigest()
    assert h == expected
    assert len(h) == 64


def test_hash_none_is_empty() -> None:
    assert _hash(None) == ""


def test_hash_deterministic_across_dict_order() -> None:
    h1 = _hash({"x": 1, "y": 2})
    h2 = _hash({"y": 2, "x": 1})
    assert h1 == h2


def test_mcp_context_required_fields() -> None:
    """Required fields are always emitted; optional fields only when populated."""
    ctx = mcp_invocation_context(
        server_name="fs-server",
        tool_name="read_file",
        tool_input={"path": "/tmp/x"},
    )
    # Required
    assert ctx["bijotel.mcp.server_name"] == "fs-server"
    assert ctx["bijotel.mcp.tool_name"] == "read_file"
    assert len(ctx["bijotel.mcp.tool_input_hash"]) == 64
    assert ctx["bijotel.mcp.tool_output_hash"] == ""  # None → empty
    assert ctx["bijotel.mcp.status"] == "success"
    assert ctx["bijotel.mcp.transport"] == "unknown"
    assert ctx["bijotel.mcp.duration_ms"] == 0.0
    # Optionals NOT set when empty
    assert "bijotel.mcp.server_version" not in ctx
    assert "bijotel.mcp.caller" not in ctx
    assert "bijotel.mcp.error_type" not in ctx


def test_mcp_context_with_all_optionals() -> None:
    ctx = mcp_invocation_context(
        server_name="srv",
        tool_name="t",
        tool_input={"x": 1},
        tool_output={"ok": True},
        server_version="1.2.3",
        caller="agent_42",
        duration_ms=45.2,
        status="success",
        transport="stdio",
    )
    assert ctx["bijotel.mcp.server_version"] == "1.2.3"
    assert ctx["bijotel.mcp.caller"] == "agent_42"
    assert ctx["bijotel.mcp.duration_ms"] == 45.2
    assert ctx["bijotel.mcp.transport"] == "stdio"
    assert ctx["bijotel.mcp.tool_output_hash"] != ""


def test_mcp_context_error_status() -> None:
    ctx = mcp_invocation_context(
        server_name="srv",
        tool_name="t",
        tool_input={"x": 1},
        tool_output=None,
        status="error",
        error_type="FileNotFoundError",
    )
    assert ctx["bijotel.mcp.status"] == "error"
    assert ctx["bijotel.mcp.error_type"] == "FileNotFoundError"
    assert ctx["bijotel.mcp.tool_output_hash"] == ""


def test_mcp_attrs_vocabulary_complete() -> None:
    """All keys produced by ``mcp_invocation_context`` are documented in MCP_ATTRS."""
    ctx = mcp_invocation_context(
        server_name="s", tool_name="t", tool_input={"a": 1},
        tool_output={"b": 2}, server_version="v", caller="c",
        duration_ms=1.0, error_type="E",
    )
    for key in ctx:
        assert key in MCP_ATTRS, f"{key} not in vocabulary"


# ─── 2. Public API exports ─────────────────────────────────────────────


def test_public_api_exports() -> None:
    """v2.12.0 promotes these names to the top-level ``bijotel`` package."""
    import bijotel
    for name in ("MCPInstrumentor", "mcp_invocation_context"):
        assert hasattr(bijotel, name), f"bijotel.{name} missing"
        assert name in bijotel.__all__


def test_mcp_module_imports_without_mcp_sdk() -> None:
    """``import bijotel.mcp`` must NOT require the ``mcp`` SDK to be installed.

    The SDK is only needed when ``.instrument()`` is actually called.
    """
    import bijotel.mcp  # noqa: F401


# ─── 3. Instrumentor (with a stubbed mcp module) ───────────────────────


@pytest.fixture
def stub_mcp_module(monkeypatch):
    """Inject a minimal ``mcp`` module with a stub ``ClientSession`` so the
    instrumentor can patch it without needing the real SDK installed.
    """
    # Build a fresh stub each call (so the sentinel doesn't leak between tests).
    stub_mcp = types.ModuleType("mcp")

    class StubClientSession:
        async def call_tool(self, name, arguments=None, **kwargs):
            # Test override per-instance:
            if hasattr(self, "_raise"):
                raise self._raise
            return self._return

    stub_mcp.ClientSession = StubClientSession
    monkeypatch.setitem(sys.modules, "mcp", stub_mcp)

    # Reload bijotel.mcp.instrumentor so it picks up the stub if cached.
    import bijotel.mcp.instrumentor as instr_mod
    importlib.reload(instr_mod)

    return stub_mcp, StubClientSession


def test_instrument_patches_call_tool(stub_mcp_module):
    stub_mcp, StubClientSession = stub_mcp_module  # noqa: N806
    from bijotel.mcp import MCPInstrumentor

    original = StubClientSession.call_tool
    MCPInstrumentor().instrument()
    # call_tool replaced
    assert StubClientSession.call_tool is not original
    assert getattr(StubClientSession, "_bijotel_mcp_patched", False) is True


def test_instrument_is_idempotent(stub_mcp_module):
    stub_mcp, StubClientSession = stub_mcp_module  # noqa: N806
    from bijotel.mcp import MCPInstrumentor

    MCPInstrumentor().instrument()
    patched_once = StubClientSession.call_tool
    MCPInstrumentor().instrument()
    assert StubClientSession.call_tool is patched_once


def test_patched_call_emits_span_with_attributes(stub_mcp_module):
    """Verify the instrumented call_tool sets the right span attributes."""
    import asyncio

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    # Fresh provider + exporter for span capture
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    stub_mcp, StubClientSession = stub_mcp_module  # noqa: N806
    from bijotel.mcp import MCPInstrumentor

    MCPInstrumentor().instrument()

    session = StubClientSession()
    session._return = {"content": "hello"}
    session.server_name = "fs-server"

    asyncio.run(session.call_tool("read_file", {"path": "/tmp/x"}))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected 1 span, got {len(spans)}"
    span = spans[0]
    assert span.name == "mcp.tool.read_file"
    attrs = dict(span.attributes)
    assert attrs["bijotel.mcp.server_name"] == "fs-server"
    assert attrs["bijotel.mcp.tool_name"] == "read_file"
    assert attrs["bijotel.mcp.status"] == "success"
    assert len(attrs["bijotel.mcp.tool_input_hash"]) == 64
    assert len(attrs["bijotel.mcp.tool_output_hash"]) == 64
    assert attrs["bijotel.mcp.duration_ms"] >= 0.0


def test_patched_call_captures_errors(stub_mcp_module):
    import asyncio

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    stub_mcp, StubClientSession = stub_mcp_module  # noqa: N806
    from bijotel.mcp import MCPInstrumentor
    MCPInstrumentor().instrument()

    session = StubClientSession()
    session._raise = FileNotFoundError("nope")

    with pytest.raises(FileNotFoundError):
        asyncio.run(session.call_tool("read_file", {"path": "/nope"}))

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes)
    assert attrs["bijotel.mcp.status"] == "error"
    assert attrs["bijotel.mcp.error_type"] == "FileNotFoundError"
    # Output hash is empty for failed calls
    assert attrs["bijotel.mcp.tool_output_hash"] == ""


def test_instrument_without_mcp_sdk_raises_clean_import_error(monkeypatch):
    """If MCP SDK isn't installed, .instrument() raises a helpful ImportError."""
    # Ensure mcp is NOT importable
    monkeypatch.setitem(sys.modules, "mcp", None)
    # Force fresh import attempt
    import bijotel.mcp.instrumentor as instr_mod
    importlib.reload(instr_mod)
    from bijotel.mcp import MCPInstrumentor

    with pytest.raises(ImportError, match="MCP SDK not installed"):
        MCPInstrumentor().instrument()
