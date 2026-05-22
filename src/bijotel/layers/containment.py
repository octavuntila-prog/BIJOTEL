"""F18 / Combo D: Containment Guard — orchestrates Permitted + Safe + Sealed.

The catalog's "Combo D — Agent Containment Stack" composes:

* **#11 Forensic-First**: tamper-evident chain (BIJOTEL ``HmacChainSpanProcessor``)
* **#10 Compliance-as-code**: policy gate (BIJOTEL ``PolicyEngine``)
* **#5 AST-First Safety**: structural code verification (BIJOTEL
  ``ASTSafetyChecker``)

This module wires the three into a single ``evaluate_action(action)``
call that answers the three-question safety frame in one shot:

1. **Is it permitted?** → :class:`PolicyEngine` evaluation
2. **Is it safe?** → :class:`ASTSafetyChecker` scan of any code blocks
3. **Was it sealed?** → record the containment decision as a synthetic
   audit event (caller's responsibility to wire into the chain; this
   module's contract is to PRODUCE the decision record, not to write it)

Returns a :class:`ContainmentDecision` carrying all three answers plus
warnings and violations. Designed to compose cleanly:

* No ``ASTSafetyChecker`` → ``safe=True`` by definition (no code check
  performed)
* No chain processor → ``sealed=None`` (decision produced but not
  recorded)
* PolicyEngine denies → ``permitted=False``, short-circuit (don't
  bother running AST check)

Not a SpanProcessor itself — it sits ABOVE the SpanProcessor stack as
a host-app integration point. The host calls
``containment.evaluate_action(action)`` before invoking the LLM (or
tool), reads the decision, and either proceeds or refuses.

Provenance: combination pattern from BIJUTERII catalog "Combo D";
implementation BIJOTEL-original.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bijotel.policy.decision import Decision, PolicyDeniedError

if TYPE_CHECKING:
    from bijotel.layers.ast_safety import ASTSafetyChecker
    from bijotel.policy.engine import PolicyEngine

_LOG = logging.getLogger("bijotel.containment")


@dataclass
class ContainmentDecision:
    """Three-question result of :meth:`ContainmentGuard.evaluate_action`.

    Attributes:
        permitted: ``True`` if policy gate allowed (or warned).
            ``False`` if any policy rule denied.
        safe: ``True`` if AST scan found no critical violations, OR if
            no AST checker is configured. ``False`` if at least one
            ``"critical"``-severity violation was detected.
        sealed: ``True`` if the containment decision was successfully
            recorded to the chain; ``None`` if no chain processor is
            configured (record is produced but not persisted by this
            module — host integration writes it).
        policy_decision: The aggregate :class:`Decision` from
            ``PolicyEngine.evaluate``. Always present.
        policy_warnings: List of warn-mode :class:`Decision` objects
            collected by the engine before any deny short-circuit.
        ast_violations: List of :class:`ASTViolation` from AST scan.
        seal_record: Dict suitable for serializing as a chain entry
            (caller writes to chain via their preferred mechanism).
    """

    permitted: bool
    safe: bool
    sealed: bool | None
    policy_decision: Decision
    policy_warnings: list[Decision] = field(default_factory=list)
    ast_violations: list[Any] = field(default_factory=list)  # ASTViolation
    seal_record: dict[str, Any] = field(default_factory=dict)

    @property
    def all_clear(self) -> bool:
        """True iff permitted AND safe (sealed is informational, not blocking)."""
        return self.permitted and self.safe


class ContainmentGuard:
    """Combo D orchestrator: Policy + AST + chain seal in one call.

    Args:
        policy_engine: REQUIRED. The :class:`PolicyEngine` to consult.
        ast_checker: OPTIONAL. If set, runs ``check_prompt`` over the
            action's messages to detect critical code-safety violations.
            If ``None``, ``safe=True`` by definition.
        chain_writer: OPTIONAL. Callable ``(record: dict) -> bool`` that
            persists the containment decision to an audit chain. Return
            value indicates success. If ``None``, ``sealed=None`` in
            the result (the decision is produced + returned but NOT
            persisted by this module).
    """

    def __init__(
        self,
        policy_engine: PolicyEngine,
        *,
        ast_checker: ASTSafetyChecker | None = None,
        chain_writer: callable | None = None,
    ) -> None:
        self._engine = policy_engine
        self._ast = ast_checker
        self._writer = chain_writer

    def evaluate_action(self, action: dict) -> ContainmentDecision:
        """Run policy + AST + seal, return rich :class:`ContainmentDecision`.

        ``action`` shape mirrors the PolicyEngine request shape (must
        carry ``"messages"`` and optionally ``"model"`` /
        ``"max_tokens"``). Any extra keys are preserved into the seal
        record for forensic value.

        Short-circuit behavior:
          * If policy DENIES, skip AST check (the action won't proceed
            anyway); ``safe`` defaults to ``True``.
          * If policy ALLOWS but AST finds critical violations,
            ``permitted=True`` but ``safe=False``; the host decides
            whether to proceed (perhaps in warn-mode deployments) or
            block (in strict deployments).
        """
        # Step 1: Policy gate
        policy_decision, policy_warnings = self._engine.evaluate(action)
        permitted = not policy_decision.is_deny

        # Step 2: AST safety (skip if denied; we know it won't run)
        ast_violations: list[Any] = []
        safe = True
        if permitted and self._ast is not None:
            messages = action.get("messages", [])
            prompt_text = _flatten_messages_text(messages)
            if prompt_text:
                ast_violations = list(self._ast.check_prompt(prompt_text))
                # safe iff no critical violations
                safe = not any(v.severity == "critical" for v in ast_violations)

        # Step 3: Build seal record
        seal_record = {
            "permitted": permitted,
            "safe": safe,
            "policy_decision": {
                "state": policy_decision.state.value,
                "rule": policy_decision.rule,
                "reason": policy_decision.reason,
            },
            "policy_warnings": [
                {"rule": w.rule, "reason": w.reason} for w in policy_warnings
            ],
            "ast_violations": [
                {
                    "pattern": v.pattern_name,
                    "language": v.language,
                    "line": v.line,
                    "severity": v.severity,
                }
                for v in ast_violations
            ],
            "action_keys": sorted(action.keys()),
        }

        # Step 4: Optionally seal
        sealed: bool | None = None
        if self._writer is not None:
            try:
                sealed = bool(self._writer(seal_record))
            except Exception as e:
                _LOG.error(
                    "containment chain_writer failed: %s: %s",
                    type(e).__name__,
                    e,
                )
                sealed = False

        return ContainmentDecision(
            permitted=permitted,
            safe=safe,
            sealed=sealed,
            policy_decision=policy_decision,
            policy_warnings=list(policy_warnings),
            ast_violations=ast_violations,
            seal_record=seal_record,
        )

    def guard_or_raise(self, action: dict) -> ContainmentDecision:
        """Convenience: evaluate, raise :class:`PolicyDeniedError` if not permitted.

        Useful as a one-liner gate in host code::

            decision = guard.guard_or_raise(request)
            # if we get here, action is permitted (may still be unsafe;
            # check decision.safe explicitly if AST blocking is desired)
            response = client.messages.create(**request)
        """
        decision = self.evaluate_action(action)
        if not decision.permitted:
            pd = decision.policy_decision
            raise PolicyDeniedError(
                rule=pd.rule or "containment",
                reason=pd.reason or "denied by containment guard",
            )
        return decision


def _flatten_messages_text(messages: Sequence[dict] | str) -> str:
    """Same shape-extraction as F11 / AST safety / fingerprint."""
    if isinstance(messages, str):
        return messages
    parts: list[str] = []
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict):
                        t = p.get("text") or p.get("content") or ""
                        if isinstance(t, str):
                            parts.append(t)
    return " ".join(p for p in parts if p)
