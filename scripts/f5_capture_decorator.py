"""F5 integration: ARA-style mock cu custom extractors.

Demonstrează:
1. Decorator pe metodă async cu signature non-Anthropic (cfg dataclass)
2. Custom extractors pentru request + response
3. Extra attrs (org_id, agent_id) propagated în chain
4. Chain valid + CAS dedup pe input identic

NU necesită ANTHROPIC_API_KEY — folosește mock LLMClient deterministic.
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel import trace_genai
from bijotel.processors import (
    CasSpanProcessor,
    HmacChainSpanProcessor,
    cas_stats,
    verify_chain,
)

DB_PATH = Path(__file__).resolve().parent.parent / "f5_decorator.db"


# ARA-style data models (mock)
@dataclass
class ModelConfig:
    model_id: str
    max_tokens: int


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost: float


# ARA-style LLMClient (mock)
class MockLLMClient:
    """Mock with same shape as ARA's LLMClient.complete()."""

    @trace_genai(
        name="ara.llm.call",
        provider="ara",
        request_extractor=lambda kw: {
            "model": kw["cfg"].model_id,
            "messages": kw["messages"],
            "max_tokens": kw["cfg"].max_tokens,
        },
        response_extractor=lambda resp: {
            "response_model": resp.model,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
        },
        extra_attrs={"ara.deployment": "test"},
    )
    async def complete(
        self,
        *,
        agent_id: str,
        messages: list,
        cfg: ModelConfig,
        org_id: str,
    ) -> LLMResponse:
        # Simulate work
        await asyncio.sleep(0.01)
        # Deterministic output based on input length
        out_text = f"echo: {len(str(messages))} chars"
        return LLMResponse(
            text=out_text,
            model=cfg.model_id,
            input_tokens=len(str(messages)) // 4,
            output_tokens=len(out_text) // 4,
            cost=0.0001,
        )


async def main() -> int:
    if DB_PATH.exists():
        DB_PATH.unlink()

    secret = secrets.token_bytes(32)
    print(f"Secret (hex): {secret.hex()}", file=sys.stderr)

    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=DB_PATH, secret_key=secret)
    )
    provider.add_span_processor(CasSpanProcessor(db_path=DB_PATH))
    trace.set_tracer_provider(provider)

    client = MockLLMClient()
    cfg = ModelConfig(model_id="claude-haiku-4-5", max_tokens=100)

    # 4 calls: 2 cu input identic, 2 cu input diferit
    print("=== Call 1: identical input (instance 1) ===", file=sys.stderr)
    await client.complete(
        agent_id="agent_42",
        messages=[{"role": "user", "content": "test query"}],
        cfg=cfg,
        org_id="org_alpha",
    )

    print("=== Call 2: identical input (instance 2) ===", file=sys.stderr)
    await client.complete(
        agent_id="agent_42",
        messages=[{"role": "user", "content": "test query"}],
        cfg=cfg,
        org_id="org_alpha",
    )

    print("=== Call 3: different input ===", file=sys.stderr)
    await client.complete(
        agent_id="agent_43",
        messages=[{"role": "user", "content": "different query"}],
        cfg=cfg,
        org_id="org_beta",
    )

    print("=== Call 4: yet another input ===", file=sys.stderr)
    await client.complete(
        agent_id="agent_44",
        messages=[{"role": "user", "content": "third unique query"}],
        cfg=cfg,
        org_id="org_alpha",
    )

    provider.shutdown()

    # Verify chain
    print("\n=== Chain verify ===", file=sys.stderr)
    valid, seq, reason = verify_chain(DB_PATH, secret)
    if valid:
        print("Chain VALID.", file=sys.stderr)
    else:
        print(f"Chain BROKEN at seq={seq}: {reason}", file=sys.stderr)
        return 2

    # CAS stats
    stats = cas_stats(DB_PATH)
    print("\n=== CAS stats ===", file=sys.stderr)
    print(f"Unique bodies: {stats['unique_bodies']} (expected: 3)", file=sys.stderr)
    print(f"Total refs: {stats['total_refs']} (expected: 4)", file=sys.stderr)
    print(f"Dedup factor: {stats['dedup_factor']:.2f}x", file=sys.stderr)

    # Span dump
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT seq, span_name, semantic_body_hash FROM chain ORDER BY seq"
        ).fetchall()
        print("\n=== Spans ===", file=sys.stderr)
        for seq, name, sem_hash in rows:
            print(
                f"  seq={seq}  name={name}  sem_hash={sem_hash[:16]}...",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
