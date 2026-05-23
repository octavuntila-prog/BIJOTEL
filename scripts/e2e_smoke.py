"""E2E smoke: full BIJOTEL stack on real Anthropic calls + CLI verify.

Compune toate piesele:
  - HmacChainSpanProcessor (F2): tamper-evident chain
  - CasSpanProcessor (F3): content-addressable input dedup
  - guard() cu policy rules (F4): cost cap + model allowlist + daily budget warn
  - AnthropicInstrumentor: tracer real upstream
  - @trace_genai (F5): demonstrează decorator pe wrapper custom
  - bijotel CLI (F6): verify/stats/list/inspect via subprocess

Flow:
  Phase 1 — guard() + AnthropicInstrumentor
    1. Allow (cheap)
    2. Allow same input  -> CAS dedup ref_count=2
    3. Deny cost cap     -> synthetic span bijotel.policy.gate
    4. Deny allowlist    -> synthetic span bijotel.policy.gate

  Phase 2 — @trace_genai pe wrapper care face client.messages.create
    5. Allow             -> outer span myapp.llm.call + inner anthropic.chat

  Phase 3 — CLI subprocess
    bijotel verify / stats / list / list --blocked / list --rule / inspect 1

Cost: ~$0.001 (5 haiku calls @ <50 tokens fiecare).
Requires: ANTHROPIC_API_KEY (env var sau BIJOTEL/.env).
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Walk upward looking for .env — robust pentru worktrees
    # (în worktree, parent.parent NU e BIJOTEL/ ci .claude/worktrees/<wt>/).
    _here = Path(__file__).resolve().parent
    for _candidate in (_here, *_here.parents):
        _env_path = _candidate / ".env"
        if _env_path.exists():
            load_dotenv(dotenv_path=_env_path, override=True)
            break
except ImportError:
    pass

import anthropic
from opentelemetry import trace
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
from opentelemetry.sdk.trace import TracerProvider

from bijotel import trace_genai
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

DB_PATH = Path(__file__).resolve().parent.parent / "e2e_smoke.db"
HAIKU = "claude-haiku-4-5-20251001"


def hr(label: str) -> None:
    print(f"\n{'=' * 8} {label} {'=' * 8}", file=sys.stderr)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY not set. "
            "Provide via env or BIJOTEL/.env.",
            file=sys.stderr,
        )
        return 1

    if DB_PATH.exists():
        DB_PATH.unlink()

    secret = secrets.token_bytes(32)
    secret_hex = secret.hex()
    print(f"HMAC secret (hex): {secret_hex}", file=sys.stderr)
    print(f"DB: {DB_PATH}", file=sys.stderr)

    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=DB_PATH, secret_key=secret)
    )
    provider.add_span_processor(CasSpanProcessor(db_path=DB_PATH))
    trace.set_tracer_provider(provider)
    AnthropicInstrumentor().instrument()

    _aig_h = ({"cf-aig-authorization": f"Bearer {os.environ['CLOUDFLARE_AIG_TOKEN']}"} if os.environ.get("CLOUDFLARE_AIG_TOKEN") else None)
    client = anthropic.Anthropic(
        base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
        default_headers=_aig_h,
    )

    policy = [
        cost_per_call_max(usd=0.01),
        daily_token_budget(tokens=10_000_000, db_path=DB_PATH, mode="warn"),
        model_allowlist(HAIKU),
    ]
    guarded_create = guard(client.messages.create, policy=policy)

    def attempt(label: str, **kwargs: object) -> None:
        hr(label)
        try:
            response = guarded_create(**kwargs)
            print(
                f"  ALLOWED. stop_reason={response.stop_reason}",
                file=sys.stderr,
            )
        except PolicyDeniedError as e:
            print(f"  DENIED by '{e.rule}': {e.reason}", file=sys.stderr)

    # Phase 1
    attempt(
        "Call 1: cheap haiku (expect ALLOW)",
        model=HAIKU,
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'a'."}],
    )
    attempt(
        "Call 2: identical input (expect ALLOW + CAS dedup)",
        model=HAIKU,
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'a'."}],
    )
    attempt(
        "Call 3: high max_tokens (expect DENY cost)",
        model=HAIKU,
        max_tokens=4096,
        messages=[{"role": "user", "content": "tell me a long story"}],
    )
    attempt(
        "Call 4: opus model (expect DENY allowlist)",
        model="claude-opus-4-7",
        max_tokens=20,
        messages=[{"role": "user", "content": "hi"}],
    )

    # Phase 2: @trace_genai on a custom wrapper
    hr("Phase 2: @trace_genai pe wrapper custom (no policy)")

    @trace_genai(provider="anthropic-custom", name="myapp.llm.call")
    def my_wrapper(*, model: str, messages: list, max_tokens: int):
        return client.messages.create(
            model=model, messages=messages, max_tokens=max_tokens
        )

    resp = my_wrapper(
        model=HAIKU,
        max_tokens=20,
        messages=[{"role": "user", "content": "Say 'b'."}],
    )
    print(
        f"  trace_genai call OK. stop_reason={resp.stop_reason}",
        file=sys.stderr,
    )

    provider.shutdown()
    AnthropicInstrumentor().uninstrument()

    # Phase 3a: in-process verify
    hr("Chain verify (in-process)")
    valid, seq, reason = verify_chain(DB_PATH, secret)
    if not valid:
        print(f"  BROKEN at seq={seq}: {reason}", file=sys.stderr)
        return 2
    print("  Chain VALID.", file=sys.stderr)

    cas = cas_stats(DB_PATH)
    print(
        f"  CAS: unique={cas['unique_bodies']} refs={cas['total_refs']} "
        f"dedup={cas['dedup_factor']:.2f}x",
        file=sys.stderr,
    )

    # Phase 3b: CLI via subprocess (foloseste entry point bijotel)
    env = {**os.environ, "BIJOTEL_HMAC_SECRET": secret_hex}
    # Prefer entry point ("bijotel") din venv Scripts/bin
    venv_bin = Path(sys.executable).parent
    cli_path = venv_bin / ("bijotel.exe" if os.name == "nt" else "bijotel")
    if cli_path.exists():
        bijotel_cmd: list[str] = [str(cli_path)]
    elif which := shutil.which("bijotel"):
        bijotel_cmd = [which]
    else:
        bijotel_cmd = [sys.executable, "-m", "bijotel.cli.main"]

    def run_cli(args: list[str]) -> int:
        full = bijotel_cmd + args
        hr(f"CLI: bijotel {' '.join(args)}")
        result = subprocess.run(full, env=env, capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.stdout.flush()
        if result.stderr:
            sys.stderr.write(result.stderr)
        return result.returncode

    if run_cli(["verify", "--db", str(DB_PATH)]) != 0:
        return 3
    if run_cli(["stats", "--db", str(DB_PATH)]) != 0:
        return 3
    if run_cli(["list", "--db", str(DB_PATH)]) != 0:
        return 3
    if run_cli(["list", "--db", str(DB_PATH), "--blocked"]) != 0:
        return 3
    if run_cli(
        ["list", "--db", str(DB_PATH), "--rule", "cost_per_call_max"]
    ) != 0:
        return 3
    if run_cli(["inspect", "--db", str(DB_PATH), "1"]) != 0:
        return 3

    hr("E2E smoke OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
