"""``/policy/*`` routes — inspect and exercise the in-process ``PolicyEngine``.

Endpoints:
  * ``GET  /policy/rules``     list active rules + introspected config
  * ``POST /policy/evaluate``  run a request through the engine, return decision

The engine is taken from ``request.app.state.policy_engine`` — set by
:func:`bijotel.api.app.create_app`. If the host didn't pass one,
``create_app`` substitutes a small default set (see ``DEFAULT_RULES``
below) so the endpoint always has something to talk about — this matches
the "honest about deferred work" principle: an empty rules list looks
identical to a misconfigured one.

Rule introspection is **best-effort**. Each rule factory in
``bijotel.policy.rules`` returns a closure; we read its ``__closure__``
freevars to surface ``mode`` / counts / limits where possible. Unknown
attributes are silently dropped — we never crash the route just because a
rule's signature changed.

Provenance: BIJOTEL-original, v1.1.0.
"""

from __future__ import annotations

import contextlib
import inspect
import time
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from bijotel.api.models import (
    PolicyEvaluateRequest,
    PolicyEvaluateResponse,
    PolicyRulesResponse,
    PolicyRuleSummary,
    PolicyWarning,
)

router = APIRouter(prefix="/policy", tags=["policy"])


# ───────────────────────── introspection helpers ─────────────────────────


def _closure_vars(rule: Any) -> dict[str, Any]:
    """Return a dict of {freevar_name: value} for a closure-style rule.

    Returns ``{}`` if ``rule`` isn't a closure or has no freevars.
    """
    try:
        cv = inspect.getclosurevars(rule)
    except TypeError:
        return {}
    return dict(cv.nonlocals)


def _summarize_rule(rule: Any) -> PolicyRuleSummary:
    """Best-effort summary of one rule — name, mode, detail dict.

    Handles the rule families currently in ``bijotel.policy.rules`` plus a
    generic fallback. Detail keys are conservative — only fields we can
    introspect cheaply and reliably are exposed.

    Naming: every rule returned by a ``*_deny`` / ``*_check`` / ``*_limit``
    factory is an inner closure literally named ``rule``. Reading
    ``__qualname__`` (which always includes the factory name as prefix
    before ``.<locals>.rule``) gives us the public name the user expects.
    """
    qname = getattr(rule, "__qualname__", "") or getattr(rule, "__name__", "rule")
    name = qname.split(".<locals>.")[0] if "<locals>" in qname else qname

    cv = _closure_vars(rule)
    mode: str | None = cv.get("mode")
    detail: dict[str, Any] = {}

    # prompt_pattern_deny: closure has `matcher` (CompiledPatternMatcher) with .compiled list
    matcher = cv.get("matcher")
    if matcher is not None:
        compiled = getattr(matcher, "compiled", None)
        if compiled is not None and hasattr(compiled, "__len__"):
            detail["patterns"] = len(compiled)

    # pii_detection: closure has `compiled` list directly
    compiled = cv.get("compiled")
    if compiled is not None and hasattr(compiled, "__len__"):
        detail.setdefault("patterns", len(compiled))

    # Generic fallback — anything iterable named "patterns" in nonlocals
    patterns = cv.get("patterns")
    if (
        "patterns" not in detail
        and isinstance(patterns, Iterable)
        and not isinstance(patterns, (str, bytes))
    ):
        with contextlib.suppress(TypeError):
            detail["patterns"] = len(list(patterns))

    # Numeric / collection knobs across rule families
    for key in (
        "usd",
        "usd_limit",
        "daily_token_limit",
        "calls_per_minute",
        "max_tokens",
        "allowed",
        "allowed_models",
        "allowed_versions",
    ):
        if key in cv and cv[key] is not None:
            v = cv[key]
            # Show count instead of raw list for collections (lighter payload)
            if isinstance(v, (list, tuple, set, frozenset)):
                detail[key + "_count"] = len(v)
            else:
                detail[key] = v

    return PolicyRuleSummary(name=name, mode=mode, detail=detail)


# ───────────────────────── GET /policy/rules ─────────────────────────


@router.get(
    "/rules",
    response_model=PolicyRulesResponse,
    summary="List active policy rules",
)
def policy_rules(request: Request) -> PolicyRulesResponse:
    """Return all rules currently configured on the engine.

    Hits the ``PolicyEngine._rules`` list directly — that's a stable
    attribute the engine has exposed since v0.5.0. If the engine has no
    rules, returns ``{rules: [], total: 0}``.
    """
    engine = getattr(request.app.state, "policy_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail="No PolicyEngine configured. Pass policy_engine= to create_app().",
        )
    rules_list = list(getattr(engine, "_rules", []))
    summaries = [_summarize_rule(r) for r in rules_list]
    return PolicyRulesResponse(rules=summaries, total=len(summaries))


# ───────────────────────── POST /policy/evaluate ─────────────────────────


@router.post(
    "/evaluate",
    response_model=PolicyEvaluateResponse,
    summary="Evaluate a request through the policy engine",
)
def policy_evaluate(
    payload: PolicyEvaluateRequest, request: Request
) -> PolicyEvaluateResponse:
    """Run ``payload`` through ``PolicyEngine.evaluate`` and return the result.

    Mirrors what BIJOTEL would do at pre-call time inside ``@guard`` — but
    isolated from any LLM call. Useful for:
      * dashboards: "would this prompt have been blocked?"
      * CI tests on prompt corpora
      * red-teaming probes
    """
    engine = getattr(request.app.state, "policy_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail="No PolicyEngine configured. Pass policy_engine= to create_app().",
        )

    req_dict: dict[str, Any] = {"messages": payload.messages}
    if payload.model is not None:
        req_dict["model"] = payload.model
    if payload.max_tokens is not None:
        req_dict["max_tokens"] = payload.max_tokens

    t0 = time.perf_counter()
    decision, warnings = engine.evaluate(req_dict)
    dt_ms = (time.perf_counter() - t0) * 1000

    return PolicyEvaluateResponse(
        decision="deny" if decision.is_deny else "allow",
        denied=decision.is_deny,
        deny_rule=decision.rule if decision.is_deny else None,
        deny_reason=decision.reason if decision.is_deny else None,
        warnings=[PolicyWarning(rule=w.rule, reason=w.reason) for w in warnings],
        evaluation_ms=round(dt_ms, 3),
    )


__all__ = ["router"]
