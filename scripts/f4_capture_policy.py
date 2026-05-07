"""F4 integration: 5 calls cu policy gate.

Expected outcomes:
- Call 1: ALLOW (cheap haiku call)
- Call 2: DENY (cost cap exceeded — high max_tokens)
- Call 3: DENY (model not in allowlist — opus)
- Call 4: ALLOW (another cheap haiku call)
- Call 5: DENY (cost cap exceeded again)

Verifies: chain integrity, blocked spans sigilate în chain (audit completeness),
synthetic spans cu bijotel.blocked=true.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import sys
from pathlib import Path

# Optional .env loading
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=True)
except ImportError:
    pass

import anthropic
from opentelemetry import trace
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
from opentelemetry.sdk.trace import TracerProvider

from bijotel.policy import (
    PolicyDeniedError,
    cost_per_call_max,
    daily_token_budget,
    guard,
    model_allowlist,
)
from bijotel.processors import (
    CasSpanProcessor,
    HmacChainSpanProcessor,
    cas_stats,
    verify_chain,
)

DB_PATH = Path(__file__).resolve().parent.parent / "f4_bijotel.db"


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    if DB_PATH.exists():
        DB_PATH.unlink()

    secret = secrets.token_bytes(32)

    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=DB_PATH, secret_key=secret)
    )
    provider.add_span_processor(CasSpanProcessor(db_path=DB_PATH))
    trace.set_tracer_provider(provider)
    AnthropicInstrumentor().instrument()

    client = anthropic.Anthropic()

    # Policy: cap cost, daily budget warn (large), model whitelist (haiku only)
    policy = [
        cost_per_call_max(usd=0.01),  # Strict cap
        daily_token_budget(tokens=10_000_000, db_path=DB_PATH, mode="warn"),
        model_allowlist("claude-haiku-4-5-20251001"),
    ]
    guarded_create = guard(client.messages.create, policy=policy)

    def attempt(label: str, **kwargs: object) -> None:
        print(f"\n=== {label} ===", file=sys.stderr)
        try:
            response = guarded_create(**kwargs)
            print(f"  ALLOWED. stop_reason={response.stop_reason}", file=sys.stderr)
        except PolicyDeniedError as e:
            print(f"  DENIED by '{e.rule}': {e.reason}", file=sys.stderr)

    # Call 1: ALLOWED
    attempt(
        "Call 1: cheap haiku (expect ALLOW)",
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'a'."}],
    )

    # Call 2: DENIED (cost cap)
    # haiku output: 4096 * $0.0040/1k = $0.01638 > $0.01 limit
    attempt(
        "Call 2: large output budget (expect DENY cost)",
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": "tell me a story"}],
    )

    # Call 3: DENIED (wrong model)
    attempt(
        "Call 3: opus model not allowed (expect DENY allowlist)",
        model="claude-opus-4-7",
        max_tokens=20,
        messages=[{"role": "user", "content": "hi"}],
    )

    # Call 4: ALLOWED
    attempt(
        "Call 4: another cheap call (expect ALLOW)",
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'b'."}],
    )

    # Call 5: DENIED (cost cap din nou)
    attempt(
        "Call 5: another expensive call (expect DENY cost)",
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": "huge response please"}],
    )

    provider.shutdown()

    # Verify chain integrity
    print("\n=== Chain verify ===", file=sys.stderr)
    valid, seq, reason = verify_chain(DB_PATH, secret)
    if valid:
        print("Chain VALID.", file=sys.stderr)
    else:
        print(f"Chain BROKEN at seq={seq}: {reason}", file=sys.stderr)
        return 2

    # Span breakdown
    with sqlite3.connect(DB_PATH) as conn:
        all_spans = conn.execute(
            "SELECT span_name, canonical_body FROM chain ORDER BY seq"
        ).fetchall()
        blocked = sum(1 for _, b in all_spans if b'"bijotel.blocked":true' in b)
        anthropic_real = sum(1 for n, _ in all_spans if n == "anthropic.chat")
        gate = sum(1 for n, _ in all_spans if n == "bijotel.policy.gate")
        print("\n=== Span breakdown ===", file=sys.stderr)
        print(f"Total spans: {len(all_spans)}", file=sys.stderr)
        print(
            f"  anthropic.chat (allowed, real call): {anthropic_real}",
            file=sys.stderr,
        )
        print(
            f"  bijotel.policy.gate (denied, synthetic): {gate}", file=sys.stderr
        )
        print(
            f"  blocked count (bijotel.blocked=true): {blocked}", file=sys.stderr
        )

    print("\n=== CAS stats ===", file=sys.stderr)
    stats = cas_stats(DB_PATH)
    print(f"Unique bodies: {stats['unique_bodies']}", file=sys.stderr)
    print(f"Total refs: {stats['total_refs']}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
