"""Default prompt patterns for jailbreak / prompt injection detection (F11).

Patterns adapted from substrate-guard's ``agent_safety.rego`` ``dangerous_patterns``
concept (separate project at ``89.167.66.225``, read-only access 2026-05-10).
The substrate-guard version targets filesystem/network/shell actions; this
BIJOTEL adaptation targets LLM prompts (instruction overrides, system-prompt
extraction, role overrides, jailbreak framings, encoding bypass).

Defaults are conservative — designed for ``warn``-mode deployment first, then
``deny``-mode after a no-false-positives review. False positives are easier to
diagnose than false negatives in this domain (security tradeoff favors detection).

Five attack categories covered:

1. **Instruction override** — "ignore previous instructions", "forget all"
2. **System prompt extraction** — "reveal your system prompt"
3. **Role override** — "you are now a different AI"
4. **Jailbreak framing** — "DAN mode", "developer mode", "hypothetically"
5. **Encoding bypass** — base64, rot13, reverse-text wrappers
"""

from __future__ import annotations

import re

# Default jailbreak / prompt injection patterns.
# Conservative wording — pattern matches the COMMON form, not edge cases.
# Case-insensitive matching applied by default in CompiledPatternMatcher.
DEFAULT_JAILBREAK_PATTERNS: list[str] = [
    # 1. Direct instruction override
    r"ignore (all )?(previous|prior|above) (instructions?|prompts?|rules?)",
    r"disregard (all )?(previous|prior|above) (instructions?|prompts?|rules?)",
    r"forget (everything|all|previous|prior|above)",
    # 2. System prompt extraction attempts
    r"(reveal|show|print|output|repeat) (your |the )?system prompt",
    r"(reveal|show|print|output|repeat) (your |the )?(initial|original) (instructions?|prompt)",
    r"what (are|were) (your|the) (instructions?|rules?|system prompt)",
    # 3. Role override
    r"you are (now |actually |really )?(a )?(different|new|another) (assistant|AI|model)",
    r"pretend (you are|to be) (a )?different",
    r"act as if you (were|are) ",
    # 4. Jailbreak framing
    r"(DAN|do anything now) mode",
    r"(developer|debug|admin|god) mode",
    r"hypothetically",  # broad — useful as warn-mode signal, debatable as deny
    # 5. Encoding bypass attempts
    r"base64\s*[:=]",
    r"rot13",
    r"reverse the (text|string|message)",
]


def get_default_patterns() -> list[str]:
    """Return a fresh copy of default jailbreak/injection patterns.

    Returns a copy so callers can extend ``patterns + extras`` without
    mutating the module-level ``DEFAULT_JAILBREAK_PATTERNS`` list.
    """
    return list(DEFAULT_JAILBREAK_PATTERNS)


class CompiledPatternMatcher:
    """Lazy-compiled regex matcher for prompt patterns.

    Pattern compilation deferred until first ``match()`` call — keeps
    rule construction cheap (most policy engines build rules eagerly even
    if most are never evaluated for some span).

    Args:
        patterns: List of raw regex strings.
        flags: ``re`` flags. Default ``re.IGNORECASE`` (most attacks use
            mixed case to evade naive string matching).
    """

    def __init__(
        self, patterns: list[str], flags: int = re.IGNORECASE
    ) -> None:
        self._raw_patterns = patterns
        self._flags = flags
        self._compiled: list[re.Pattern] | None = None

    @property
    def compiled(self) -> list[re.Pattern]:
        """Lazy-compile patterns on first access."""
        if self._compiled is None:
            self._compiled = [
                re.compile(p, self._flags) for p in self._raw_patterns
            ]
        return self._compiled

    def match(self, text: str) -> str | None:
        """Return first matching raw pattern string, or ``None``.

        Returns the **raw pattern string** (not the compiled regex), so
        callers can include it in audit messages without leaking the
        compiled object into chain.db.
        """
        for raw, compiled in zip(self._raw_patterns, self.compiled, strict=False):
            if compiled.search(text):
                return raw
        return None
