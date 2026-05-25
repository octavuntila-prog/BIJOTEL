"""F1 schema discovery: rulează 3 calls Anthropic distincte, capturează spans.

Output: f1_spans.jsonl — un span JSON per linie, ready pentru F2 referință.

NU se comite f1_spans.jsonl — adăugat în .gitignore.

API key sourcing (în ordine):
  1. ANTHROPIC_API_KEY env var (deja setat în shell)
  2. .env file la nivel BIJOTEL/ (loaded via python-dotenv dacă disponibil)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Optional .env loading — dacă python-dotenv e instalat, încarcă BIJOTEL/.env
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(dotenv_path=_env_path, override=True)
except ImportError:
    pass

import anthropic
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

import bijotel

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "f1_spans.jsonl"


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY not set.\n"
            "  Option 1: export ANTHROPIC_API_KEY=sk-ant-... (bash)\n"
            "             $env:ANTHROPIC_API_KEY='sk-ant-...' (PowerShell)\n"
            "  Option 2: create BIJOTEL/.env with ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        return 1

    output_file = OUTPUT_PATH.open("w", encoding="utf-8")

    bijotel.init(service_name="bijotel-f1-discovery", output=output_file)
    AnthropicInstrumentor().instrument()

    _aig_h = (
        {"cf-aig-authorization": f"Bearer {os.environ['CLOUDFLARE_AIG_TOKEN']}"}
        if os.environ.get("CLOUDFLARE_AIG_TOKEN")
        else None
    )
    client = anthropic.Anthropic(
        base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
        default_headers=_aig_h,
    )

    print("=== Test 1: Basic Messages call ===", file=sys.stderr)
    resp1 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": "Say 'hello' and nothing else."}],
    )
    print(f"Response: {resp1.content[0].text!r}", file=sys.stderr)

    print("=== Test 2: Streaming Messages call ===", file=sys.stderr)
    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        messages=[{"role": "user", "content": "Count: 1, 2, 3."}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", file=sys.stderr, flush=True)
        print(file=sys.stderr)

    print("=== Test 3: Tool use Messages call ===", file=sys.stderr)
    resp3 = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        tools=[
            {
                "name": "get_weather",
                "description": "Get weather for a location",
                "input_schema": {
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            }
        ],
        messages=[{"role": "user", "content": "What's the weather in Bucharest?"}],
    )
    print(f"Response stop_reason: {resp3.stop_reason}", file=sys.stderr)

    bijotel.shutdown()
    output_file.close()

    print(f"\nSpans captured to {OUTPUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
