"""F3 integration: 4 calls (2 cu input identic) -> chain 4 + CAS 3 (1 ref_count=2).

Demonstrează:
- Chain conține 4 rows (toate spans sigilate)
- CAS conține 3 unique bodies (1 dedup pe call 1+2 cu input identic)
- chain.semantic_body_hash[1] == chain.semantic_body_hash[2]
- chain.canonical_hash[1] != chain.canonical_hash[2] (output varies, even if input same)

NU se comite f3_bijotel.db (în .gitignore: *.db).
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

from bijotel.processors import (
    CasSpanProcessor,
    HmacChainSpanProcessor,
    cas_stats,
    verify_chain,
)

DB_PATH = Path(__file__).resolve().parent.parent / "f3_bijotel.db"


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    if DB_PATH.exists():
        DB_PATH.unlink()

    secret = secrets.token_bytes(32)
    print(f"Secret (hex): {secret.hex()}", file=sys.stderr)

    provider = TracerProvider()
    # Same DB pentru chain + CAS (recomandat pentru convenience, NU atomicity)
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=DB_PATH, secret_key=secret)
    )
    provider.add_span_processor(CasSpanProcessor(db_path=DB_PATH))
    trace.set_tracer_provider(provider)
    AnthropicInstrumentor().instrument()

    client = anthropic.Anthropic()

    # Calls 1 + 2: input identic, output va fi diferit (Claude e non-determinist)
    print("=== Call 1: Identical prompt (instance 1) ===", file=sys.stderr)
    client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "Say a number between 1 and 1000."}],
    )

    print("=== Call 2: Identical prompt (instance 2) ===", file=sys.stderr)
    client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "Say a number between 1 and 1000."}],
    )

    # Calls 3 + 4: prompts diferite
    print("=== Call 3: Different prompt ===", file=sys.stderr)
    client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "What's the capital of France?"}],
    )

    print("=== Call 4: Different prompt ===", file=sys.stderr)
    client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'hello world'."}],
    )

    provider.shutdown()

    # Verify chain integrity
    valid, seq, reason = verify_chain(DB_PATH, secret)
    print("\n=== Chain verify ===", file=sys.stderr)
    if valid:
        print("Chain VALID.", file=sys.stderr)
    else:
        print(f"Chain BROKEN at seq={seq}: {reason}", file=sys.stderr)
        return 2

    # Verify chain rows count
    with sqlite3.connect(DB_PATH) as conn:
        chain_count = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
        print(f"Chain rows: {chain_count} (expected: 4)", file=sys.stderr)

    # Verify CAS dedup
    print("\n=== CAS stats ===", file=sys.stderr)
    stats = cas_stats(DB_PATH)
    print(f"Unique bodies: {stats['unique_bodies']} (expected: 3)", file=sys.stderr)
    print(f"Total refs: {stats['total_refs']} (expected: 4)", file=sys.stderr)
    print(f"Dedup factor: {stats['dedup_factor']:.2f}x", file=sys.stderr)

    # Cross-reference: chain.semantic_body_hash -> CAS.body_hash
    print("\n=== Chain -> CAS cross-ref ===", file=sys.stderr)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT chain.seq, chain.semantic_body_hash,
                   chain.canonical_hash, cas.ref_count
            FROM chain
            LEFT JOIN cas ON chain.semantic_body_hash = cas.body_hash
            ORDER BY chain.seq
        """).fetchall()
        for seq, sem_hash, can_hash, ref_count in rows:
            print(
                f"  seq={seq}  "
                f"sem_hash={sem_hash[:16]}...  "
                f"can_hash={can_hash[:16]}...  "
                f"ref_count={ref_count}",
                file=sys.stderr,
            )

    # Validare comportamentală: seq 1+2 same sem_hash, different can_hash
    print("\n=== Behavioral validation (calls 1+2 same input) ===", file=sys.stderr)
    with sqlite3.connect(DB_PATH) as conn:
        r1 = conn.execute(
            "SELECT semantic_body_hash, canonical_hash FROM chain WHERE seq = 1"
        ).fetchone()
        r2 = conn.execute(
            "SELECT semantic_body_hash, canonical_hash FROM chain WHERE seq = 2"
        ).fetchone()
    same_sem = r1[0] == r2[0]
    diff_can = r1[1] != r2[1]
    print(f"  semantic_body_hash[1] == semantic_body_hash[2]: {same_sem}", file=sys.stderr)
    print(f"  canonical_hash[1]     != canonical_hash[2]:     {diff_can}", file=sys.stderr)
    if not (same_sem and diff_can):
        print("  WARNING: expected same sem + diff can (input-only dedup)", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
