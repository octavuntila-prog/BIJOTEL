"""PolicyEngine: evaluate list of rules against a request."""

from __future__ import annotations

from collections.abc import Callable

from bijotel.policy.decision import Decision

Rule = Callable[[dict], Decision]


class PolicyEngine:
    """Evaluate list of rules against request, aggregate decisions.

    Aggregation logic:
        - First DENY -> return immediately (short-circuit, don't run remaining rules)
        - All ALLOWs -> return Decision.allow()
        - Mix of ALLOWs and WARNs -> return list of WARNs (caller emits all in span)
    """

    def __init__(self, rules: list[Rule]) -> None:
        self._rules = list(rules)

    def evaluate(self, request: dict) -> tuple[Decision, list[Decision]]:
        """Run rules against request.

        Returns:
            (final_decision, warnings_list)

            final_decision:
                - Decision.deny(...) if any rule denied (first deny wins)
                - Decision.allow() if all allow (warnings don't block)
            warnings_list: All Decision.warn() results (empty if no warnings).
        """
        warnings: list[Decision] = []
        for rule in self._rules:
            result = rule(request)
            if result.is_deny:
                return result, warnings  # Short-circuit on first deny
            if result.is_warn:
                warnings.append(result)
        return Decision.allow(), warnings
