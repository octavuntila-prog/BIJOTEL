"""Shared CLI helpers used across the bijotel cmd_*.py modules.

Extracted from the v2.5.0-era ``commands.py`` monolith. Anything called
by more than one subcommand handler lives here: HMAC secret resolution,
canonical body parsing, status/cost calculation, range-flag parsing.

The previous ``commands.py`` re-exports every name in this module so
test imports like ``from bijotel.cli.commands import _calc_cost``
continue to work unchanged.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from bijotel.policy.prices import DEFAULT_PRICES


def _resolve_secret(args: argparse.Namespace) -> bytes | None:
    """Get HMAC secret from --secret-hex flag or BIJOTEL_HMAC_SECRET env var.

    Returns:
        bytes if available, None otherwise (caller decides if required).
    """
    hex_str = args.secret_hex or os.environ.get("BIJOTEL_HMAC_SECRET")
    if not hex_str:
        return None
    try:
        return bytes.fromhex(hex_str)
    except ValueError:
        print(f"ERROR: Invalid hex in secret: {hex_str[:8]}...", file=sys.stderr)
        sys.exit(2)


def _parse_canonical_body(body: bytes) -> dict:
    """Parse canonical_body BLOB to dict."""
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _detect_status(body_dict: dict) -> str:
    """Return BLOCKED / WARN / ALLOWED based on attributes."""
    attrs = body_dict.get("attributes", {})
    if attrs.get("bijotel.blocked") is True:
        return "BLOCKED"
    if "bijotel.policy.warning" in attrs:
        return "WARN"
    return "ALLOWED"


def _calc_cost(body_dict: dict) -> str:
    """Calculate real cost from gen_ai.usage attrs.

    Returns:
        - "-"            if usage tokens missing (cannot compute)
        - "?<model>"     if model not in DEFAULT_PRICES (unknown pricing).
                         Includes model name fragment so user knows WHICH model
                         is missing — actionable feedback vs opaque "?".
        - "$0.0000"      ONLY when both input and output tokens are 0
                         (e.g. blocked span with no real call)
        - "<$0.0001"     when computed cost > 0 but < $0.0001
                         (preserves signal: real call, just tiny — vs zero)
        - "$0.NNNN"      4-decimal format for normal costs

    Fixes v0.2.0 inconsistency where:
        - Sonnet 4 (claude-sonnet-4-20250514) returned "?" because not in
          price table (now added in prices.py).
        - Tiny Haiku calls returned "$0.0000" (indistinguishable from
          truly-zero blocked spans). Now distinguished as "<$0.0001".
    """
    attrs = body_dict.get("attributes", {})
    model = attrs.get("gen_ai.request.model", "")
    input_tokens = attrs.get("gen_ai.usage.input_tokens")
    output_tokens = attrs.get("gen_ai.usage.output_tokens")

    if input_tokens is None or output_tokens is None:
        return "-"
    if model not in DEFAULT_PRICES:
        # Include short model fragment for actionable feedback
        model_short = model[:30] if model else "(none)"
        return f"?{model_short}"

    cost = (
        input_tokens * DEFAULT_PRICES[model]["input"]
        + output_tokens * DEFAULT_PRICES[model]["output"]
    ) / 1000

    if cost == 0:
        return "$0.0000"
    if cost < 0.0001:
        # Real call, but rounds to zero at 4 decimals — distinguish signal
        return "<$0.0001"
    return f"${cost:.4f}"


def _format_time(timestamp_ns: int) -> str:
    """Convert ns timestamp to ISO8601 UTC string."""
    seconds = timestamp_ns / 1e9
    dt = datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_range_args(args: argparse.Namespace) -> dict:
    """Translate --since/--until/--range/--last argparse output into the
    keyword arguments verify_chain/export_chain/chain_range_summary accept.

    Returns an empty dict if no range filters are passed (full-chain).
    """
    out: dict = {}
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    rng = getattr(args, "range", None)
    last_n = getattr(args, "last", None)

    if since:
        try:
            dt = datetime.datetime.strptime(since, "%Y-%m-%d").replace(
                tzinfo=datetime.UTC
            )
        except ValueError as e:
            raise SystemExit(f"ERROR: --since must be YYYY-MM-DD, got {since!r}") from e
        out["since_ns"] = int(dt.timestamp() * 1e9)
    if until:
        try:
            dt = datetime.datetime.strptime(until, "%Y-%m-%d").replace(
                tzinfo=datetime.UTC
            )
            # Inclusive upper bound — end of that calendar day UTC.
            dt = dt + datetime.timedelta(days=1, microseconds=-1)
        except ValueError as e:
            raise SystemExit(f"ERROR: --until must be YYYY-MM-DD, got {until!r}") from e
        out["until_ns"] = int(dt.timestamp() * 1e9)
    if rng:
        try:
            a, b = rng.split(":", 1)
            out["seq_start"] = int(a) if a else None
            out["seq_end"] = int(b) if b else None
        except ValueError as e:
            raise SystemExit(f"ERROR: --range must be A:B, got {rng!r}") from e
    if last_n is not None:
        out["last_n"] = int(last_n)
    return out
