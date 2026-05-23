"""F2 integration test: 3 calls Anthropic + HmacChainSpanProcessor + verify_chain.

Output: f2_chain.db cu 3 rows în chain table + raport verify_chain.
NU se comite f2_chain.db (în .gitignore: *.db).
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

# Optional .env loading - dacă python-dotenv e instalat, încarcă BIJOTEL/.env
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

from bijotel.processors import HmacChainSpanProcessor, verify_chain

DB_PATH = Path(__file__).resolve().parent.parent / "f2_chain.db"


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    if DB_PATH.exists():
        DB_PATH.unlink()  # Fresh start

    secret = secrets.token_bytes(32)
    print(f"Secret (hex, save for verify): {secret.hex()}", file=sys.stderr)

    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=DB_PATH, secret_key=secret)
    )
    trace.set_tracer_provider(provider)
    AnthropicInstrumentor().instrument()

    _aig_h = ({"cf-aig-authorization": f"Bearer {os.environ['CLOUDFLARE_AIG_TOKEN']}"} if os.environ.get("CLOUDFLARE_AIG_TOKEN") else None)
    client = anthropic.Anthropic(
        base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
        default_headers=_aig_h,
    )

    print("=== Call 1: Basic Messages ===", file=sys.stderr)
    client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'a' once."}],
    )

    print("=== Call 2: Basic Messages ===", file=sys.stderr)
    client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'b' once."}],
    )

    print("=== Call 3: Basic Messages ===", file=sys.stderr)
    client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'c' once."}],
    )

    provider.shutdown()

    print("\n=== Verify chain ===", file=sys.stderr)
    valid, seq, reason = verify_chain(DB_PATH, secret)
    if valid:
        print(f"Chain VALID. {DB_PATH} integrity confirmed.", file=sys.stderr)
        return 0
    else:
        print(f"Chain BROKEN at seq={seq}: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
