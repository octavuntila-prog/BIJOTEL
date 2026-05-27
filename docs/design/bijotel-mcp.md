# bijotel-mcp: HMAC-chained MCP invocation traces

**Status:** Draft v0.1 — 2026-05-27 (BIJOTEL v2.12.0 development)
**Authors:** Octavian Untilă
**Related:** [`docs/design/cross-org-federation.md`](cross-org-federation.md),
[`docs/threat-model.md`](../threat-model.md)

---

## 1. Problem

MCP (Model Context Protocol) tool invocations are fire-and-forget: a client
calls `mcp_client.call_tool(name, args)`, the server returns a result, and no
tamper-evident record of the exchange is kept locally. Existing telemetry
(server access logs, application logs) is mutable, not chained, and not
cryptographically integrity-bound.

Empirically:

- **40+ CVEs filed against MCP servers** between Jan–Apr 2026 (per
  `cve.mitre.org` filters on "mcp" + "model context protocol"). Active
  exploitation, not theoretical.
- **NSA Cybersecurity Information Sheet (May 2026)** explicitly names
  MCP and recommends "audit logging into SIEM" — but does **not** specify
  cryptographic integrity for that audit trail.
- **MCP Registry (Linux Foundation, May 2026)** provides *static metadata*
  (who published what server), but no runtime invocation trail.

The gap is: **who called what, with what inputs, producing what outputs,
and is that record tamper-evident.**

## 2. Solution

Extend BIJOTEL to seal MCP invocations into the same HMAC chain alongside
existing LLM calls. One unified audit trail covers:

- **LLM calls** (existing, since v0.1.0): OTel `gen_ai.*` spans, sealed by
  `HmacChainSpanProcessor`.
- **MCP tool invocations** (new, v2.12.0): OTel `mcp.*` spans, sealed by the
  same processor.

The seam is OpenTelemetry. The seal mechanism is unchanged. The deliverable
is a new instrumentor + a small attribute vocabulary.

## 3. Attribute vocabulary

The attributes BIJOTEL captures per MCP invocation:

| Attribute | Type | Required | Notes |
|---|---|---|---|
| `bijotel.mcp.server_name` | str | ✓ | MCP server identity (e.g. `filesystem-server`) |
| `bijotel.mcp.server_version` | str |   | server version, when known |
| `bijotel.mcp.tool_name` | str | ✓ | tool being invoked (e.g. `read_file`) |
| `bijotel.mcp.tool_input_hash` | str | ✓ | SHA-256 of canonicalized arguments (hex) |
| `bijotel.mcp.tool_output_hash` | str |   | SHA-256 of result (hex). Empty if call failed. |
| `bijotel.mcp.caller` | str |   | who invoked (agent name, user id, process) |
| `bijotel.mcp.duration_ms` | float |   | invocation duration in milliseconds |
| `bijotel.mcp.status` | str | ✓ | `success` / `error` |
| `bijotel.mcp.error_type` | str |   | exception class name, when status=error |
| `bijotel.mcp.transport` | str |   | `stdio` / `sse` / `streamable-http` / `unknown` |

**Rationale for hashes (not raw content):**

- Raw tool arguments often contain secrets (file paths, credentials, prompts).
- Hashes preserve forensic value (verify against known-good blob later)
  without exposing content in the chain.
- Same pattern as `gen_ai.input.messages` redaction at the `redact_input=True`
  policy boundary.

## 4. Integration patterns

### A) Python MCP SDK hook (primary)

```python
from bijotel.mcp import MCPInstrumentor

MCPInstrumentor().instrument()
# All subsequent ClientSession.call_tool() invocations are sealed.
```

Monkey-patches `mcp.ClientSession.call_tool` (the canonical Python MCP
client) so every async tool call emits an OTel span with `bijotel.mcp.*`
attributes, which `HmacChainSpanProcessor` then seals into `chain.db`.

Idempotent — calling `.instrument()` twice is a no-op (skill same as
`opentelemetry-instrumentation-anthropic`).

### B) Standalone proxy (for non-Python servers)

```bash
bijotel-mcp-proxy --upstream stdio://path/to/server --db chain.db
```

Out of scope for v2.12.0. Documented as future work — the proxy
intercepts stdio MCP traffic, emits spans, forwards. Useful when the
MCP server is written in another language (Go, Node) and the client
can't be instrumented in-process.

### C) Go collector extension (future)

`bijotel-collector` already accepts OTLP. If an MCP client emits
`bijotel.mcp.*` spans via standard OTel exporter, the Go collector seals
them automatically — no Python required at all.

This is the deployment model for production MCP servers that don't run
in Python.

## 5. Relationship to other ecosystem pieces

### vs MCP Registry (Linux Foundation)

- Registry: *static* — "this server exists and is version X" (publishing
  authority).
- bijotel-mcp: *runtime* — "this server was called at time T with input
  hash H1, producing output hash H2".

Complementary, not overlapping. Both layers needed for full provenance.

### vs NSA CSI on MCP security (May 2026)

NSA recommends "audit logging into SIEM" without specifying crypto.
bijotel-mcp provides tamper-evident audit logging — adds HMAC integrity
*on top of* what NSA recommends. Cite NSA as **category air cover**
("regulator says this category matters"), not endorsement.

### vs prompt injection detection (BIJOTEL F11 patterns)

F11 patterns inspect *prompt content* for known jailbreak strings.
bijotel-mcp inspects *invocations* (who called what). Both can coexist:
F11 might block an MCP tool call whose `tool_input` matches an injection
pattern, while bijotel-mcp seals the *attempt* into the chain regardless
of allow/deny.

## 6. Security model

### What bijotel-mcp proves (per sealed entry)

- Tool X was invoked at time T (timestamp, ±clock drift).
- Input had hash H1 and (if successful) output had hash H2.
- This invocation links to a specific predecessor via HMAC chain.
- Operator signed the export (Ed25519, if exported).

### What bijotel-mcp does **not** prove

- Tool X is safe to call — that's the policy gate (OPA/Rego, BIJOTEL
  PolicyEngine), not the audit.
- Input was not adversarial — that's prompt injection detection (F11 +
  containment), not the audit.
- MCP server identity is genuine — that's the Registry's namespace
  verification, not the audit.
- The hashed input/output bytes were ever stored anywhere — content
  recovery requires a separate CAS layer or out-of-band storage.

### Threat model

| Adversary capability | bijotel-mcp protection |
|---|---|
| Modify MCP server response after the fact | ✓ caught (output hash mismatch) |
| Insert fake MCP invocation in chain | ✓ caught (HMAC) |
| Replay an old MCP invocation | ✓ caught (timestamps + prev_hash chain) |
| Compromise MCP client before BIJOTEL seal | ✗ not in scope (defense-in-depth) |
| Compromise BIJOTEL HMAC secret | ✗ requires Ed25519 export + Rekor anchor for external auditor |

## 7. Out of scope for v2.12.0

- **Proxy mode** (Integration pattern B) — future work, separate binary
  (`bijotel-mcp-proxy`). Pattern A (in-process instrumentor) covers
  100% of Python-side MCP usage, which is the immediate need.
- **CAS storage of input/output bodies** — only hashes are sealed.
  Operators who want full-content forensic recovery must stand up their
  own CAS layer (or extend `bijotel.processors.CasSpanProcessor` to read
  `bijotel.mcp.*` attributes — orthogonal change, can land later without
  touching this design).
- **Policy gates on MCP** (e.g. "deny call to `write_file` when caller is
  user X") — that's PolicyEngine work, separate ticket.
- **Cross-org federation of MCP invocations** — v2.11 federation already
  handles arbitrary spans; once `bijotel.mcp.*` spans are in the chain,
  they're federated automatically. Nothing new needed.

## 8. Versioning

- **v2.12.0** ships: instrumentor + attribute vocabulary + tests + docs.
- **v2.13.0** (planned): CLI `bijotel mcp inspect` for chain.db filtering.
- **v3.0.0** (speculative): proxy mode + multi-language client support.

## 9. Validation criteria

A v2.12.0 deploy is considered DONE when:

1. `pip install bijotel[mcp]` installs `mcp` SDK + bijotel cleanly.
2. `MCPInstrumentor().instrument()` patches `ClientSession.call_tool`.
3. Subsequent `await session.call_tool(...)` calls emit spans with
   `bijotel.mcp.tool_name` attribute (verified via test).
4. Chain entry has the MCP attributes after seal.
5. `bijotel verify --db chain.db` PASSes on a chain containing MCP spans.
6. Tests cover: success path, error path, hash determinism, redaction.

## 10. References

- [MCP spec — Model Context Protocol](https://modelcontextprotocol.io/)
- [NSA CSI on MCP security, May 2026]
  (https://www.nsa.gov/Press-Room/News-Highlights/Article/...) — exact
  URL added once verified
- [Linux Foundation MCP Registry](https://github.com/modelcontextprotocol/registry)
- BIJOTEL v2.9.0 — Rekor anchoring of chain heads
- BIJOTEL v2.11.0 — Cross-org federation client
- IETF draft-sharif-agent-audit-trail-00 — partial overlap; AAT covers
  agent actions broadly, bijotel-mcp is MCP-specific
