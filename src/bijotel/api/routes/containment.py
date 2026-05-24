"""``/containment/*`` — Combo D three-question gate.

One endpoint: ``POST /containment/evaluate``.

Composes :class:`bijotel.layers.containment.ContainmentGuard` over the
host's :class:`PolicyEngine` and (optionally) an
:class:`ASTSafetyChecker`. Returns the three-question result in one
shot: **permitted?** (policy) · **safe?** (AST) · **sealed?** (chain
writer, optional).

Provenance: BIJOTEL-original, v1.7.0. Closes the "Combo D code ships,
nothing invokes it" gap flagged by the Day-14 audit (2026-05-24).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from bijotel.api.models import (
    ASTViolationItem,
    ContainmentEvaluateRequest,
    ContainmentEvaluateResponse,
    PolicyWarning,
)

router = APIRouter(prefix="/containment", tags=["containment"])

_LOG = logging.getLogger("bijotel.api.containment")


def _resolve_guard(request: Request):
    """Return the ContainmentGuard for this app, building lazily if needed.

    Resolution order:

    1. ``app.state.containment_guard`` — host pre-built one. Use it.
    2. Build on-demand from ``app.state.policy_engine`` plus an
       :class:`ASTSafetyChecker` (if the ``[ast]`` extra is installed).
       The ``ContainmentGuard`` is cached on app state so the second
       call doesn't re-init AST grammars.

    The lazy path means a host that just wires a PolicyEngine still
    gets a working containment endpoint — no extra config needed.
    """
    g = getattr(request.app.state, "containment_guard", None)
    if g is not None:
        return g

    engine = getattr(request.app.state, "policy_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No PolicyEngine configured. ContainmentGuard requires one. "
                "Pass policy_engine= to create_app() or attach a "
                "ContainmentGuard at app.state.containment_guard."
            ),
        )

    from bijotel.layers.containment import ContainmentGuard

    # AST is optional — graceful skip if the [ast] extra isn't installed.
    ast_checker = None
    try:
        from bijotel.layers.ast_safety import ASTSafetyChecker

        ast_checker = ASTSafetyChecker(languages=("python", "bash"))
    except ImportError:
        ast_checker = None
    except Exception as e:  # pragma: no cover - defensive
        _LOG.warning(
            "ASTSafetyChecker init failed (%s); containment runs without AST.",
            e,
        )
        ast_checker = None

    guard = ContainmentGuard(policy_engine=engine, ast_checker=ast_checker)
    # Cache so the next call doesn't re-init tree-sitter grammars.
    request.app.state.containment_guard = guard
    return guard


@router.post(
    "/evaluate",
    response_model=ContainmentEvaluateResponse,
    summary="Three-question containment gate (Combo D)",
)
def containment_evaluate(
    payload: ContainmentEvaluateRequest, request: Request
) -> ContainmentEvaluateResponse:
    """Run Combo D: permitted? → safe? → sealed? in one shot.

    Mirrors what BIJOTEL would do at pre-call time when the host uses
    :class:`ContainmentGuard` as the gate (rather than calling
    ``PolicyEngine.evaluate`` directly). Useful for:

    * dashboards: "would this action have been contained?"
    * pre-prod CI on prompt corpora that include code blocks
    * red-team probes that combine jailbreak text with dangerous code
    """
    guard = _resolve_guard(request)

    # Build the action dict — same shape PolicyEngine expects, plus any
    # extras the caller wants preserved into the seal record.
    action: dict[str, Any] = {"messages": payload.messages}
    if payload.model is not None:
        action["model"] = payload.model
    if payload.max_tokens is not None:
        action["max_tokens"] = payload.max_tokens
    if payload.extra:
        # extras land flat — but never overwrite the canonical keys above.
        for k, v in payload.extra.items():
            action.setdefault(k, v)

    t0 = time.perf_counter()
    decision = guard.evaluate_action(action)
    dt_ms = (time.perf_counter() - t0) * 1000

    return ContainmentEvaluateResponse(
        permitted=decision.permitted,
        safe=decision.safe,
        sealed=decision.sealed,
        all_clear=decision.all_clear,
        policy_decision=decision.policy_decision.state.value,
        policy_warnings=[
            PolicyWarning(rule=w.rule, reason=w.reason)
            for w in decision.policy_warnings
        ],
        ast_violations=[
            ASTViolationItem(
                pattern=v.pattern_name,
                language=v.language,
                line=v.line,
                severity=v.severity,
            )
            for v in decision.ast_violations
        ],
        seal_record=decision.seal_record,
        evaluation_ms=round(dt_ms, 3),
    )


__all__ = ["router"]
