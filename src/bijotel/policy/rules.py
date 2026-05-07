"""Built-in policy rules. Each rule is a callable: (request_dict) -> Decision."""

from __future__ import annotations

import datetime
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from bijotel.policy.decision import Decision
from bijotel.policy.prices import DEFAULT_PRICES

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
