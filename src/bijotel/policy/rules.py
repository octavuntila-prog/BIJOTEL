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
