# MCP Invocation Tracing

BIJOTEL v2.12.0 adds sealing for **MCP (Model Context Protocol) tool
invocations**. Every tool call through an MCP server can land in the
same HMAC chain as the LLM spans it shares a trace with — one unified
audit trail for both layers.

## Quick start

```python
from bijotel.mcp import MCPInstrumentor

# Patches mcp.ClientSession.call_tool() in place
MCPInstrumentor().instrument()

# Every subsequent tool call is sealed into chain.db
```

The instrumentor is idempotent — calling `.instrument()` twice is a
no-op. It does not raise into the caller's flow; an OTel hiccup will
not break an MCP call.

## Install

```bash
pip install 'bijotel[mcp]'
```

The base `pip install bijotel` does **not** require the MCP SDK. Only
`.instrument()` needs the SDK installed; importing `bijotel.mcp` is
safe without it.

## Attributes captured per invocation

| Attribute | Type | Required | Notes |
|---|---|---|---|
| `bijotel.mcp.server_name` | str | ✓ | MCP server identity |
| `bijotel.mcp.server_version` | str |   | server version, when known |
| `bijotel.mcp.tool_name` | str | ✓ | tool being invoked |
| `bijotel.mcp.tool_input_hash` | str | ✓ | SHA-256 of arguments (hex) |
| `bijotel.mcp.tool_output_hash` | str |   | SHA-256 of result. Empty if call failed. |
| `bijotel.mcp.caller` | str |   | who invoked (agent name, user id) |
| `bijotel.mcp.duration_ms` | float |   | invocation duration |
| `bijotel.mcp.status` | str | ✓ | `success` / `error` |
| `bijotel.mcp.error_type` | str |   | exception class name, when status=error |
| `bijotel.mcp.transport` | str |   | `stdio` / `sse` / `streamable-http` / `unknown` |

### Why hashes, not raw content

Tool arguments often contain secrets (file paths, credentials,
prompts). The chain stores **SHA-256 hashes only** — verify against a
known-good blob later if you need forensic recovery, without exposing
content in `chain.db`.

## What it proves

For each sealed entry:

- Tool X was called at time T (timestamp, ±clock drift).
- Input had hash H1, output had hash H2 (content integrity).
- This invocation links to a specific predecessor (chain integrity).
- Operator signed the export, if exported (Ed25519).

## What it does *not* prove

- Tool safety — that's a policy gate (OPA/Rego, BIJOTEL PolicyEngine),
  not the audit.
- Input legitimacy — that's prompt injection detection (F11 + containment).
- MCP server identity — that's the [Linux Foundation MCP Registry](https://github.com/modelcontextprotocol/registry)
  namespace verification.

## Manual span emission

If you can't monkey-patch (e.g. non-Python MCP client), build the
attributes manually:

```python
from bijotel.mcp import mcp_invocation_context
from opentelemetry import trace

tracer = trace.get_tracer("bijotel.mcp")

with tracer.start_as_current_span("mcp.tool.read_file") as span:
    attrs = mcp_invocation_context(
        server_name="fs-server",
        tool_name="read_file",
        tool_input={"path": "/tmp/x"},
        tool_output={"content": "..."},
        duration_ms=12.5,
        transport="stdio",
    )
    for k, v in attrs.items():
        span.set_attribute(k, v)
```

## Design doc

For protocol details (3 integration patterns, threat model, scope
boundaries), see [MCP Invocation Traces — Design](../design/bijotel-mcp.md).
