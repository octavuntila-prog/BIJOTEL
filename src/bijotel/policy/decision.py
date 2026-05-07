"""Decision: 3-state (allow/warn/deny) result of policy rule evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class State(Enum):
    ALLOW = "allow"
    WARN = "warn"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    """Result of a policy rule evaluation.

    States:
        ALLOW: call proceeds normally, no audit attribute added
        WARN: call proceeds, but synthetic attribute bijotel.policy.warning=<rule>
              added to span emitted by SDK. Useful for gradual enforcement
              (audit before deny).
        DENY: call blocked. Synthetic span emitted with bijotel.blocked=true.
              PolicyDeniedError raised.

    redact_input: only meaningful for DENY. When True, synthetic span stores
        SHA-256 hash of input messages instead of raw content (PII protection).
    """

    state: State
    rule: str = ""
    reason: str = ""
    redact_input: bool = False

    @classmethod
    def allow(cls) -> Decision:
        return cls(state=State.ALLOW)

    @classmethod
    def warn(cls, *, rule: str, reason: str) -> Decision:
        return cls(state=State.WARN, rule=rule, reason=reason)

    @classmethod
    def deny(cls, *, rule: str, reason: str, redact_input: bool = False) -> Decision:
        return cls(
            state=State.DENY, rule=rule, reason=reason, redact_input=redact_input
        )

    @property
    def is_allow(self) -> bool:
        return self.state == State.ALLOW

    @property
    def is_warn(self) -> bool:
        return self.state == State.WARN

    @property
    def is_deny(self) -> bool:
        return self.state == State.DENY


class PolicyDeniedError(Exception):
    """Raised when a policy rule denies a call."""

    def __init__(self, rule: str, reason: str) -> None:
        self.rule = rule
        self.reason = reason
        super().__init__(f"Policy '{rule}' denied: {reason}")
