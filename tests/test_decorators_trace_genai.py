"""Tests for @trace_genai decorator + wrap() runtime."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel import trace_genai, wrap
from bijotel.processors import HmacChainSpanProcessor

SECRET = b"x" * 32


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.db"


@pytest.fixture
def provider_with_chain(db_path: Path) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    )
    trace.set_tracer_provider(provider)
    return provider


def _mock_response(input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        model="claude-haiku-4-5",
        id="msg_test",
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=[SimpleNamespace(type="text", text="response")],
    )


# ─── Sync decorator tests ───


def test_sync_decorator_emits_span(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    @trace_genai(provider="anthropic")
    def my_call(*, model: str, messages: list, max_tokens: int) -> SimpleNamespace:
        return _mock_response()

    my_call(model="claude-haiku-4-5", messages=[{"role": "user"}], max_tokens=20)

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT canonical_body FROM chain").fetchall()
        assert len(rows) == 1
        body = rows[0][0]
        assert b'"gen_ai.provider.name":"anthropic"' in body
        assert b'"gen_ai.request.model":"claude-haiku-4-5"' in body
        assert b'"gen_ai.usage.input_tokens":10' in body
        assert b'"gen_ai.usage.output_tokens":5' in body
        assert b'"gen_ai.usage.total_tokens":15' in body


def test_sync_decorator_returns_value(
    provider_with_chain: TracerProvider,
) -> None:
    @trace_genai(provider="anthropic")
    def my_call(**_kwargs: object) -> SimpleNamespace:
        return _mock_response()

    result = my_call(model="x", messages=[], max_tokens=10)
    assert result.id == "msg_test"


def test_sync_decorator_propagates_exception(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    @trace_genai(provider="anthropic")
    def my_call(**_kwargs: object) -> None:
        raise ValueError("LLM error")

    with pytest.raises(ValueError, match="LLM error"):
        my_call(model="x", messages=[])

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        assert b'"status_code":"ERROR"' in body


# ─── Async decorator tests ───


def test_async_decorator_emits_span(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    @trace_genai(provider="anthropic")
    async def my_call(**_kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(0)  # Yield to verify async
        return _mock_response(input_tokens=20, output_tokens=8)

    asyncio.run(my_call(model="claude-haiku-4-5", messages=[], max_tokens=10))

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        assert b'"gen_ai.usage.input_tokens":20' in body
        assert b'"gen_ai.usage.output_tokens":8' in body


def test_async_decorator_propagates_exception(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    @trace_genai(provider="anthropic")
    async def my_call(**_kwargs: object) -> None:
        raise RuntimeError("async LLM error")

    with pytest.raises(RuntimeError):
        asyncio.run(my_call(model="x", messages=[]))

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        assert b'"status_code":"ERROR"' in body


# ─── Custom extractors (ARA-style) ───


def test_custom_extractors_for_ara_style_api(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """Test that custom extractors enable non-Anthropic-style signatures."""

    class LLMResponse:
        def __init__(self, text: str, model: str, in_t: int, out_t: int) -> None:
            self.text = text
            self.model = model
            self.input_tokens = in_t
            self.output_tokens = out_t

    cfg = SimpleNamespace(model_id="claude-haiku-4-5", max_tokens=100)

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
        extra_attrs={"ara.org_id": "org_test"},
    )
    async def complete(*, agent_id: str, messages: list, cfg: object) -> LLMResponse:
        return LLMResponse("response text", cfg.model_id, 15, 7)

    asyncio.run(
        complete(
            agent_id="agent_42",
            messages=[{"role": "user", "content": "hi"}],
            cfg=cfg,
        )
    )

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        assert b'"gen_ai.provider.name":"ara"' in body
        assert b'"gen_ai.request.model":"claude-haiku-4-5"' in body
        assert b'"gen_ai.usage.input_tokens":15' in body
        assert b'"ara.org_id":"org_test"' in body
        assert b"ara.llm.call" in body  # span name


def test_extractor_error_doesnt_crash_call(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """Extractor crash -> log în span attribute, dar funcția continuă."""

    def bad_extractor(_kw: dict) -> dict:
        raise ValueError("extractor broken")

    @trace_genai(provider="anthropic", request_extractor=bad_extractor)
    def my_call(**_kwargs: object) -> SimpleNamespace:
        return _mock_response()

    # Funcția ar trebui să NU crashe pentru că extractor a crashat
    result = my_call(model="x", messages=[])
    assert result.id == "msg_test"

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        body = conn.execute("SELECT canonical_body FROM chain").fetchone()[0]
        assert b"bijotel.extractor_error" in body


# ─── wrap() runtime ───


def test_wrap_runtime_equivalent_to_decorator(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    def original_call(**_kwargs: object) -> SimpleNamespace:
        return _mock_response()

    wrapped = wrap(original_call, provider="anthropic")
    wrapped(model="claude-haiku-4-5", messages=[], max_tokens=10)

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT canonical_body FROM chain").fetchall()
        assert len(rows) == 1
        assert b'"gen_ai.provider.name":"anthropic"' in rows[0][0]


def test_wrap_runtime_async(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    async def original_call(**_kwargs: object) -> SimpleNamespace:
        return _mock_response()

    wrapped = wrap(original_call, provider="anthropic")
    asyncio.run(wrapped(model="x", messages=[], max_tokens=10))

    provider_with_chain.shutdown()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT canonical_body FROM chain").fetchall()
        assert len(rows) == 1


# ─── Integration with HmacChain + verify ───


def test_decorator_spans_pass_chain_verify(
    provider_with_chain: TracerProvider, db_path: Path
) -> None:
    """5 calls via decorator -> chain valid via verify_chain."""
    from bijotel.processors import verify_chain

    @trace_genai(provider="anthropic")
    def my_call(**_kwargs: object) -> SimpleNamespace:
        return _mock_response()

    for i in range(5):
        my_call(
            model="claude-haiku-4-5",
            messages=[{"role": "user", "content": f"hi {i}"}],
            max_tokens=10,
        )

    provider_with_chain.shutdown()

    valid, seq, reason = verify_chain(db_path, SECRET)
    assert valid is True, f"Chain broken at seq={seq}: {reason}"
