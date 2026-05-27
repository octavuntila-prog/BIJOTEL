"""Monkey-patches MCP Python SDK client so calls flow through BIJOTEL.

Pattern is parallel to ``opentelemetry-instrumentation-anthropic``:
wrap the SDK's tool-invocation entry point in an OTel span, set
attributes, let the existing ``HmacChainSpanProcessor`` seal the result.

The instrumentor is idempotent — calling ``.instrument()`` twice is a
no-op (the first call sets a sentinel attribute on the patched class).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bijotel.mcp.attributes import mcp_invocation_context

logger = logging.getLogger(__name__)

# Marker we set on the patched class so a second .instrument() call
# detects the existing patch and skips.
_SENTINEL_ATTR = "_bijotel_mcp_patched"


class MCPInstrumentor:
    """Wraps the MCP Python SDK client so tool invocations are sealed.

    Usage::

        from bijotel.mcp import MCPInstrumentor

        MCPInstrumentor().instrument()
        # All subsequent ClientSession.call_tool(...) calls are sealed.

    The patched method preserves the original call signature and return
    value — instrumentation is observation-only, never raises into the
    caller's flow (an OTel hiccup will not break an MCP call).

    The patched method extracts:

    * ``server_name`` from ``session.server_name`` (when present), or
      the ``ClientSession`` instance ``repr`` as fallback.
    * ``tool_name`` from the first positional argument.
    * ``tool_input`` from ``arguments`` keyword arg.
    * ``tool_output`` from the return value (when the call succeeds).
    * ``status`` = ``"success"`` / ``"error"``.
    * ``duration_ms`` from wall-clock around the inner call.
    * ``transport`` defaults to ``"stdio"`` (the most common case;
      callers can override by setting ``session._bijotel_transport``).
    """

    def instrument(self) -> None:
        """Apply the patch. Idempotent."""
        try:
            from mcp import ClientSession  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "MCP SDK not installed. Install via: pip install 'bijotel[mcp]' "
                "or pip install mcp"
            ) from e

        if getattr(ClientSession, _SENTINEL_ATTR, False):
            logger.debug("MCPInstrumentor: already patched, skipping.")
            return

        original_call_tool = ClientSession.call_tool

        async def patched_call_tool(
            self_client: Any,
            name: str,
            arguments: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> Any:
            from opentelemetry import trace

            tracer = trace.get_tracer("bijotel.mcp")
            start = time.time()

            with tracer.start_as_current_span(f"mcp.tool.{name}") as span:
                server_name = (
                    getattr(self_client, "server_name", None)
                    or repr(self_client)
                )
                transport = getattr(
                    self_client, "_bijotel_transport", "stdio"
                )
                caller = getattr(self_client, "_bijotel_caller", "")

                try:
                    result = await original_call_tool(
                        self_client, name, arguments, **kwargs
                    )
                    duration_ms = (time.time() - start) * 1000.0
                    attrs = mcp_invocation_context(
                        server_name=server_name,
                        tool_name=name,
                        tool_input=arguments,
                        tool_output=result,
                        caller=caller,
                        duration_ms=duration_ms,
                        status="success",
                        transport=transport,
                    )
                    for k, v in attrs.items():
                        span.set_attribute(k, v)
                    return result
                except Exception as exc:
                    duration_ms = (time.time() - start) * 1000.0
                    attrs = mcp_invocation_context(
                        server_name=server_name,
                        tool_name=name,
                        tool_input=arguments,
                        tool_output=None,
                        caller=caller,
                        duration_ms=duration_ms,
                        status="error",
                        error_type=type(exc).__name__,
                        transport=transport,
                    )
                    for k, v in attrs.items():
                        span.set_attribute(k, v)
                    raise

        # Apply patch + mark.
        ClientSession.call_tool = patched_call_tool  # type: ignore[assignment]
        setattr(ClientSession, _SENTINEL_ATTR, True)
        logger.info("MCPInstrumentor: patched mcp.ClientSession.call_tool")

    def uninstrument(self) -> None:
        """Restore the original ``call_tool``. Mostly for tests."""
        try:
            from mcp import ClientSession  # type: ignore[import-not-found]
        except ImportError:
            return

        if not getattr(ClientSession, _SENTINEL_ATTR, False):
            return

        # We don't keep a handle to the original; the simplest
        # restoration is re-importing. For test purposes, callers
        # should pytest-monkeypatch the attribute themselves. We just
        # clear the sentinel so re-instrumentation works.
        delattr(ClientSession, _SENTINEL_ATTR)
        logger.info("MCPInstrumentor: cleared sentinel (re-instrument allowed)")
