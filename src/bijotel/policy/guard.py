"""guard(): decorator wrapping callable with policy gate.

Note: guard expects keyword-only invocation matching Anthropic SDK API
(client.messages.create(messages=..., model=..., ...)). Positional args
NOT supported in F4 v0; documented limitation.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Callable
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind

from bijotel.policy.decision import Decision, PolicyDeniedError
from bijotel.policy.engine import PolicyEngine

Rule = Callable[[dict], Decision]


def _emit_synthetic_span(
    *,
    request: dict,
    decision_state: str,
    rule: str,
    reason: str,
    redact_input: bool,
    warnings: list[Decision],
) -> None:
    """Emit synthetic span via current TracerProvider.

    Span trece prin all SpanProcessors registered (chain, CAS, etc.).
    Visible în audit chain ca evidence of denied/warned call.
    """
    tracer = trace.get_tracer("bijotel.policy")

    # Pregătim attributes (with redaction dacă e cazul)
    messages_attr: str
    if redact_input:
        raw_messages = json.dumps(request.get("messages", []), ensure_ascii=False)
        input_hash = hashlib.sha256(raw_messages.encode("utf-8")).hexdigest()
        messages_attr = f"sha256:{input_hash}"
    else:
        messages_attr = json.dumps(request.get("messages", []), ensure_ascii=False)

    with tracer.start_as_current_span(
        name="bijotel.policy.gate",
        kind=SpanKind.CLIENT,
    ) as span:
        # Standard gen_ai.* attributes (so span trece filter-ul default)
        span.set_attribute("gen_ai.provider.name", "anthropic")
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.request.model", request.get("model", "unknown"))
        if request.get("max_tokens") is not None:
            span.set_attribute("gen_ai.request.max_tokens", request["max_tokens"])
        span.set_attribute("gen_ai.input.messages", messages_attr)

        # BIJOTEL-specific attributes
        if decision_state == "deny":
            span.set_attribute("bijotel.blocked", True)
            span.set_attribute("bijotel.policy.rule", rule)
            span.set_attribute("bijotel.policy.reason", reason)
            if redact_input:
                span.set_attribute("bijotel.policy.input_redacted", True)
        elif decision_state == "warn":
            span.set_attribute("bijotel.policy.warning", rule)
            span.set_attribute("bijotel.policy.warning_reason", reason)

        # Multi-warning case: list all warnings
        if warnings:
            span.set_attribute(
                "bijotel.policy.warnings",
                json.dumps(
                    [{"rule": w.rule, "reason": w.reason} for w in warnings]
                ),
            )


def guard(
    fn: Callable[..., Any],
    *,
    policy: list[Rule],
) -> Callable[..., Any]:
    """Wrap fn with policy gate. ALLOW -> call fn. DENY -> raise. WARN -> emit + call fn.

    Args:
        fn: Callable to wrap (typically client.messages.create).
        policy: List of rule callables.

    Returns:
        Wrapped callable. Raises PolicyDeniedError on deny.

    Note: Accepts kwargs only (Anthropic SDK convention). Positional args
    not supported in F4 v0.
    """
    engine = PolicyEngine(policy)

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> Any:
        # Request dict = kwargs (Anthropic SDK uses kwarg-only API for messages.create)
        decision, warnings = engine.evaluate(kwargs)
        if decision.is_deny:
            _emit_synthetic_span(
                request=kwargs,
                decision_state="deny",
                rule=decision.rule,
                reason=decision.reason,
                redact_input=decision.redact_input,
                warnings=warnings,
            )
            raise PolicyDeniedError(rule=decision.rule, reason=decision.reason)

        # Allow path. Emit warnings dacă există (warn-only span pre-call)
        for w in warnings:
            _emit_synthetic_span(
                request=kwargs,
                decision_state="warn",
                rule=w.rule,
                reason=w.reason,
                redact_input=False,
                warnings=[],
            )

        # Call real function (instrumentation-anthropic emits its own span)
        return fn(**kwargs)

    return wrapper
