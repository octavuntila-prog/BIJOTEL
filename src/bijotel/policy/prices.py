"""Anthropic price snapshot. USD per 1k tokens.

LAST UPDATED: 2026-05-07.
Verify current prices at https://www.anthropic.com/pricing.
Override via cost_per_call_max(usd=..., prices={...}) if outdated.
If snapshot is older than 180 days, a DeprecationWarning is emitted at import time.
"""

from __future__ import annotations

import datetime
import warnings

SNAPSHOT_DATE = datetime.date(2026, 5, 7)
SNAPSHOT_NAME = "ANTHROPIC_PRICES_2026_05"

ANTHROPIC_PRICES_2026_05: dict[str, dict[str, float]] = {
    # USD per 1k tokens (input / output)
    "claude-haiku-4-5-20251001": {"input": 0.0008, "output": 0.0040},
    "claude-haiku-4-5": {"input": 0.0008, "output": 0.0040},
    "claude-sonnet-4-6": {"input": 0.0030, "output": 0.0150},
    "claude-opus-4-7": {"input": 0.0150, "output": 0.0750},
    "claude-opus-4-6": {"input": 0.0150, "output": 0.0750},
}

DEFAULT_PRICES = ANTHROPIC_PRICES_2026_05


def _check_snapshot_age() -> None:
    """Emit DeprecationWarning if snapshot older than 180 days."""
    age_days = (datetime.date.today() - SNAPSHOT_DATE).days
    if age_days > 180:
        warnings.warn(
            f"BIJOTEL price snapshot ({SNAPSHOT_NAME}) is {age_days} days old. "
            f"Verify prices at https://www.anthropic.com/pricing. "
            f"Override via cost_per_call_max(prices=...).",
            DeprecationWarning,
            stacklevel=3,
        )


# Run check at import time
_check_snapshot_age()
