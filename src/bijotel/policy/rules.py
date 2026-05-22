"""Built-in policy rules. Each rule is a callable: (request_dict) -> Decision."""

from __future__ import annotations

import datetime
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from bijotel.policy.decision import Decision
from bijotel.policy.prices import DEFAULT_PRICES
from bijotel.policy.prompt_patterns import (
    CompiledPatternMatcher,
    get_default_patterns,
)

# Type alias pentru clarity
Rule = Callable[[dict], Decision]


def _estimate_input_tokens(messages: list) -> int:
    """Estimate input tokens from messages list.

    Heuristic: len(text) / 4. Conservative — overestimates real tokens by ~10-30%
    pentru English. Pentru non-Latin scripts, may underestimate.

    Pentru exact precision, post-call rules cu gen_ai.usage.input_tokens
    sunt necesare (NOT in F4 v0).
    """
    text = json.dumps(messages, ensure_ascii=False)
    return len(text) // 4


def cost_per_call_max(
    usd: float,
    *,
    prices: dict[str, dict[str, float]] | None = None,
    mode: str = "deny",
) -> Rule:
    """Block calls with estimated cost > usd.

    Estimation: input_tokens (estimated) * input_price + max_tokens * output_price.
    Conservative: assumes max output. Real cost <= estimate în majoritatea cazurilor.

    Args:
        usd: Maximum allowed cost per single call în USD.
        prices: Override price table. Default: DEFAULT_PRICES.
        mode: "deny" (block + raise) sau "warn" (allow + audit). Default deny.

    Returns:
        Rule callable.
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")

    price_table = prices or DEFAULT_PRICES

    def rule(request: dict) -> Decision:
        model = request.get("model", "")
        if model not in price_table:
            # Fail-safe: unknown model -> deny cu mesaj clar (NU silent allow)
            return Decision.deny(
                rule="cost_per_call_max",
                reason=(
                    f"Model {model!r} not in price table. "
                    "Provide prices=... explicitly."
                ),
            )

        input_tokens = _estimate_input_tokens(request.get("messages", []))
        max_tokens = request.get("max_tokens", 0) or 0
        cost = (
            input_tokens * price_table[model]["input"]
            + max_tokens * price_table[model]["output"]
        ) / 1000

        if cost > usd:
            reason = f"Estimated cost ${cost:.4f} exceeds limit ${usd:.4f}"
            if mode == "deny":
                return Decision.deny(rule="cost_per_call_max", reason=reason)
            return Decision.warn(rule="cost_per_call_max", reason=reason)

        return Decision.allow()

    return rule


def daily_token_budget(
    tokens: int,
    *,
    db_path: str | Path,
    mode: str = "deny",
) -> Rule:
    """Block calls when daily total tokens (estimated) > budget.

    State persisted in SQLite table `policy_daily_state(date, total_tokens)`.
    Date in UTC. Counter resets at 00:00 UTC.

    Args:
        tokens: Maximum tokens allowed per day (input + max_tokens estimate).
        db_path: SQLite path. Same DB ca HmacChain/CAS recomandat.
        mode: "deny" sau "warn".

    Returns:
        Rule callable.
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")

    db_path_resolved = Path(db_path)

    # Init table at rule construction
    with sqlite3.connect(db_path_resolved) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS policy_daily_state (
                date TEXT PRIMARY KEY,
                total_tokens INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()

    def rule(request: dict) -> Decision:
        today = datetime.datetime.now(datetime.UTC).date().isoformat()
        input_tokens = _estimate_input_tokens(request.get("messages", []))
        max_tokens = request.get("max_tokens", 0) or 0
        request_tokens = input_tokens + max_tokens

        with sqlite3.connect(db_path_resolved) as conn:
            row = conn.execute(
                "SELECT total_tokens FROM policy_daily_state WHERE date = ?",
                (today,),
            ).fetchone()
            current = row[0] if row else 0
            projected = current + request_tokens

            if projected > tokens:
                reason = (
                    f"Projected daily total {projected} tokens "
                    f"(current {current} + this call ~{request_tokens}) "
                    f"exceeds budget {tokens}"
                )
                if mode == "deny":
                    return Decision.deny(rule="daily_token_budget", reason=reason)
                return Decision.warn(rule="daily_token_budget", reason=reason)

            # Update counter (atomic via INSERT ON CONFLICT)
            conn.execute(
                """
                INSERT INTO policy_daily_state (date, total_tokens)
                VALUES (?, ?)
                ON CONFLICT(date) DO UPDATE SET total_tokens = total_tokens + ?
                """,
                (today, request_tokens, request_tokens),
            )
            conn.commit()

        return Decision.allow()

    return rule


def rate_limit_calls_per_minute(
    max_calls: int,
    *,
    db_path: str | Path,
    mode: str = "deny",
) -> Rule:
    """Block calls when rolling 60-second window already contains max_calls.

    Sliding window (NOT calendar minute reset). At evaluation time:
        1. Prune timestamps older than 60s from policy_rate_limit_state.
        2. Count remaining; if >= max_calls -> deny.
        3. Otherwise insert current timestamp -> allow.

    State persisted in SQLite table ``policy_rate_limit_state(timestamp_ns)``.
    Same DB recommended as HmacChain/CAS for unified storage.

    Pattern adapted from substrate-guard's "api_calls_last_minute > 100"
    deny rule (separate project, read-only access 2026-05-10).

    Args:
        max_calls: Maximum allowed calls in any rolling 60-second window.
        db_path: SQLite path. Same DB as HmacChain/CAS recommended.
        mode: "deny" or "warn".

    Returns:
        Rule callable.

    Note: state checked AND mutated atomically per call. Concurrent calls
    from multiple threads/processes coordinate via SQLite write lock.
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")
    if max_calls < 1:
        raise ValueError(f"max_calls must be >= 1, got {max_calls}")

    db_path_resolved = Path(db_path)

    with sqlite3.connect(db_path_resolved) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS policy_rate_limit_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_ns INTEGER NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rl_ts "
            "ON policy_rate_limit_state(timestamp_ns)"
        )
        conn.commit()

    def rule(request: dict) -> Decision:  # noqa: ARG001 (request unused for this rule)
        now_ns = time.time_ns()
        window_start_ns = now_ns - 60 * 1_000_000_000  # 60 sec in ns

        with sqlite3.connect(db_path_resolved) as conn:
            # Prune entries older than window (cleanup in same call)
            conn.execute(
                "DELETE FROM policy_rate_limit_state WHERE timestamp_ns < ?",
                (window_start_ns,),
            )
            # Count current window
            count = conn.execute(
                "SELECT COUNT(*) FROM policy_rate_limit_state"
            ).fetchone()[0]

            if count >= max_calls:
                reason = (
                    f"Rate limit exceeded: {count} calls in last 60s "
                    f"(max {max_calls})"
                )
                conn.commit()  # Commit prune even on deny path
                if mode == "deny":
                    return Decision.deny(
                        rule="rate_limit_calls_per_minute", reason=reason
                    )
                return Decision.warn(
                    rule="rate_limit_calls_per_minute", reason=reason
                )

            # Allow path: record this call's timestamp
            conn.execute(
                "INSERT INTO policy_rate_limit_state (timestamp_ns) VALUES (?)",
                (now_ns,),
            )
            conn.commit()

        return Decision.allow()

    return rule


def model_allowlist(*allowed_models: str, mode: str = "deny") -> Rule:
    """Block calls using models not in allowlist.

    Args:
        *allowed_models: Allowed model name strings (exact match).
        mode: "deny" sau "warn".

    Returns:
        Rule callable.
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")

    allowed = frozenset(allowed_models)

    def rule(request: dict) -> Decision:
        model = request.get("model", "")
        if model not in allowed:
            reason = f"Model {model!r} not in allowlist {sorted(allowed)}"
            if mode == "deny":
                return Decision.deny(rule="model_allowlist", reason=reason)
            return Decision.warn(rule="model_allowlist", reason=reason)
        return Decision.allow()

    return rule


def prompt_pattern_deny(
    patterns: list[str] | None = None,
    *,
    mode: str = "deny",
    use_defaults: bool = True,
) -> Rule:
    """Deny calls whose prompt matches known jailbreak / injection patterns.

    Inspects ``request["messages"]`` (Anthropic-style list of dicts, supports
    both string content and multipart ``[{"type": "text", "text": "..."}]``
    format). Returns ``Decision.deny`` (or ``warn``) on first matching pattern.

    Args:
        patterns: Custom regex strings. If ``None`` and ``use_defaults=True``,
            uses ``DEFAULT_JAILBREAK_PATTERNS`` only. If both provided,
            customs are appended after defaults (defaults checked first).
        mode: ``"deny"`` (block, raise via guard) or ``"warn"`` (allow, audit).
        use_defaults: Include ``DEFAULT_JAILBREAK_PATTERNS``. Default ``True``.
            Set ``False`` for purely custom matching.

    Returns:
        Rule callable matching ``PolicyEngine`` contract.

    Raises:
        ValueError: if ``mode`` is neither ``"deny"`` nor ``"warn"``.
        ValueError: if both ``patterns=None`` and ``use_defaults=False``
            (no patterns to match — would silently allow everything).

    Examples::

        # Defaults only
        rule = prompt_pattern_deny()

        # Custom + defaults (defaults checked first)
        rule = prompt_pattern_deny(
            patterns=[r"my_company_secret", r"\\bAPI[_-]KEY"]
        )

        # Custom only, no defaults
        rule = prompt_pattern_deny(
            patterns=[r"sensitive_term"], use_defaults=False
        )

        # Warn mode — audit but allow
        rule = prompt_pattern_deny(mode="warn")

    Pattern adapted from substrate-guard ``agent_safety.rego``
    ``dangerous_patterns`` concept (separate project, read-only access
    2026-05-10).
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")

    effective_patterns: list[str] = []
    if use_defaults:
        effective_patterns.extend(get_default_patterns())
    if patterns:
        effective_patterns.extend(patterns)

    if not effective_patterns:
        raise ValueError(
            "No patterns to match. Provide patterns= or set use_defaults=True."
        )

    matcher = CompiledPatternMatcher(effective_patterns)

    def rule(request: dict) -> Decision:
        # Extract user prompt text from request.messages
        messages = request.get("messages", [])

        prompt_text_parts: list[str] = []
        if isinstance(messages, str):
            # Edge case: messages already serialized as string (post-extractor)
            prompt_text_parts.append(messages)
        elif isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", "")
                if isinstance(content, str):
                    prompt_text_parts.append(content)
                elif isinstance(content, list):
                    # Multipart format (Anthropic): [{"type": "text", "text": "..."}]
                    for part in content:
                        if isinstance(part, dict):
                            txt = part.get("text", "")
                            if isinstance(txt, str):
                                prompt_text_parts.append(txt)

        prompt_text = " ".join(prompt_text_parts).strip()
        if not prompt_text:
            return Decision.allow()

        matched = matcher.match(prompt_text)
        if matched is None:
            return Decision.allow()

        # Truncate pattern in reason if very long (avoid leaking giant regex into chain)
        pattern_display = matched if len(matched) <= 80 else matched[:77] + "..."
        reason = f"prompt matched jailbreak pattern: {pattern_display!r}"
        if mode == "deny":
            return Decision.deny(rule="prompt_pattern_deny", reason=reason)
        return Decision.warn(rule="prompt_pattern_deny", reason=reason)

    return rule


# ============================================================================
# Compliance-as-code completions (F16 / Bijuteria #10)
# ============================================================================

import re as _re  # noqa: E402

# PII patterns — conservative, English-centric. Override via `patterns=`
# kwarg for domain-specific PII (medical record numbers, IBANs, etc.).
DEFAULT_PII_PATTERNS: dict[str, str] = {
    "email": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "phone_us": r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    "ssn_us": r"\b\d{3}-\d{2}-\d{4}\b",
    "credit_card": r"\b(?:\d[ -]*?){13,16}\b",  # rough; Luhn check optional
    "ipv4": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
}


def pii_detection(
    *,
    patterns: dict[str, str] | None = None,
    mode: str = "warn",
) -> Rule:
    """Detect PII patterns in prompt content. Composes additively with F11.

    Args:
        patterns: Override the default PII regex set. Keys are pattern
            names (shown in :class:`Decision` reason); values are regex
            strings compiled with case-insensitive flag. Default:
            :data:`DEFAULT_PII_PATTERNS` (email, US phone, US SSN,
            credit card, IPv4).
        mode: ``"warn"`` (audit + allow) or ``"deny"`` (block).

    Returns:
        Rule callable matching the PolicyEngine contract.

    Raises:
        ValueError: if ``mode`` is invalid.
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")

    compiled = [
        (name, _re.compile(pat, _re.IGNORECASE))
        for name, pat in (patterns or DEFAULT_PII_PATTERNS).items()
    ]

    def rule(request: dict) -> Decision:
        messages = request.get("messages", [])
        text_parts: list[str] = []
        if isinstance(messages, list):
            for m in messages:
                if not isinstance(m, dict):
                    continue
                content = m.get("content")
                if isinstance(content, str):
                    text_parts.append(content)
                elif isinstance(content, list):
                    for p in content:
                        if isinstance(p, dict):
                            t = p.get("text") or p.get("content") or ""
                            if isinstance(t, str):
                                text_parts.append(t)
        text = " ".join(text_parts)
        if not text:
            return Decision.allow()

        for pii_name, regex in compiled:
            if regex.search(text):
                reason = f"PII detected in prompt: {pii_name}"
                if mode == "deny":
                    return Decision.deny(rule="pii_detection", reason=reason)
                return Decision.warn(rule="pii_detection", reason=reason)
        return Decision.allow()

    return rule


def output_length_limit(*, max_tokens: int = 4096, mode: str = "warn") -> Rule:
    """Enforce a ceiling on requested ``max_tokens`` per call.

    Useful for: cost control without full-budget logic, safety against
    runaway generation, compliance with downstream context-window limits.

    Args:
        max_tokens: Maximum allowed value of ``request["max_tokens"]``.
            Requests with a higher max_tokens trigger the rule.
        mode: ``"deny"`` or ``"warn"``.

    Note: this checks the REQUESTED max_tokens, not actual output tokens
    (which are only known post-call). For post-call output enforcement,
    use ``cost_per_call_max`` or implement a custom rule.
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be >= 1, got {max_tokens}")

    def rule(request: dict) -> Decision:
        requested = request.get("max_tokens", 0)
        if not isinstance(requested, int) or requested <= max_tokens:
            return Decision.allow()
        reason = (
            f"Requested max_tokens={requested} exceeds policy ceiling "
            f"of {max_tokens}"
        )
        if mode == "deny":
            return Decision.deny(rule="output_length_limit", reason=reason)
        return Decision.warn(rule="output_length_limit", reason=reason)

    return rule


def model_version_pin(*, allowed_versions: list[str], mode: str = "deny") -> Rule:
    """Pin requests to specific model versions (not just model families).

    Stricter than ``model_allowlist``: must match the *exact* string in
    ``allowed_versions`` (typically date-suffixed identifiers like
    ``claude-sonnet-4-20250514``). Prevents silent provider upgrades
    where ``"claude-sonnet"`` would point to a new revision next month.

    Args:
        allowed_versions: Exact-match allowlist of model version strings.
        mode: ``"deny"`` (default; pinning is usually enforced strictly)
            or ``"warn"``.

    Raises:
        ValueError: on empty allowlist or invalid mode.
    """
    if mode not in ("deny", "warn"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")
    if not allowed_versions:
        raise ValueError("allowed_versions must be a non-empty list")

    allowed = frozenset(allowed_versions)

    def rule(request: dict) -> Decision:
        model = request.get("model", "")
        if model in allowed:
            return Decision.allow()
        reason = (
            f"Model {model!r} not in pinned versions "
            f"{sorted(allowed)} — provider silent-upgrade risk"
        )
        if mode == "deny":
            return Decision.deny(rule="model_version_pin", reason=reason)
        return Decision.warn(rule="model_version_pin", reason=reason)

    return rule
