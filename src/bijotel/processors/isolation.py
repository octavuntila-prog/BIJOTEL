"""IsolatingSpanProcessor — fence a sibling SpanProcessor's failures.

OpenTelemetry's ``SynchronousMultiSpanProcessor`` dispatches the span
lifecycle (``on_start`` / ``_on_ending`` / ``on_end``) to every registered
processor in sequence; if one raises, the remaining processors are skipped
FOR THAT SPAN. bijotel's own processors are already crash-isolated, but a
third-party processor co-registered on the *same* ``TracerProvider`` could
raise and skip the HMAC seal — the class of failure behind the 2026-06-05
GENA incident.

Wrap such a processor so its exceptions are caught, logged, and suppressed,
and chain sealing always runs::

    provider.add_span_processor(IsolatingSpanProcessor(ThirdPartyProcessor()))
    provider.add_span_processor(HmacChainSpanProcessor(...))

(audit 2026-06-08 SP-1)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opentelemetry.sdk.trace import SpanProcessor

if TYPE_CHECKING:
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import ReadableSpan, Span

_LOG = logging.getLogger("bijotel.processors.isolation")


class IsolatingSpanProcessor(SpanProcessor):
    """Delegate every SpanProcessor call to ``delegate`` but never propagate
    an exception it raises — so a misbehaving sibling cannot abort the shared
    ``SynchronousMultiSpanProcessor`` dispatch (and thereby skip chain
    sealing). Exceptions are logged at ERROR level and suppressed.
    """

    def __init__(self, delegate: SpanProcessor) -> None:
        self._delegate = delegate

    @property
    def delegate(self) -> SpanProcessor:
        """The wrapped processor (for introspection)."""
        return self._delegate

    def on_start(
        self, span: Span, parent_context: Context | None = None
    ) -> None:
        try:
            self._delegate.on_start(span, parent_context)
        except Exception:  # noqa: BLE001 — isolation is the entire purpose
            _LOG.exception("isolated SpanProcessor.on_start raised; suppressed")

    def _on_ending(self, span: Span) -> None:
        # _on_ending is an OTel SDK (>=1.42) hook called during span.end();
        # delegate it only if the wrapped processor implements its own.
        hook = getattr(self._delegate, "_on_ending", None)
        if hook is None:
            return
        try:
            hook(span)
        except Exception:  # noqa: BLE001
            _LOG.exception(
                "isolated SpanProcessor._on_ending raised; suppressed"
            )

    def on_end(self, span: ReadableSpan) -> None:
        try:
            self._delegate.on_end(span)
        except Exception:  # noqa: BLE001
            _LOG.exception("isolated SpanProcessor.on_end raised; suppressed")

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except Exception:  # noqa: BLE001
            _LOG.exception("isolated SpanProcessor.shutdown raised; suppressed")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return bool(self._delegate.force_flush(timeout_millis))
        except Exception:  # noqa: BLE001
            _LOG.exception(
                "isolated SpanProcessor.force_flush raised; suppressed"
            )
            return False
