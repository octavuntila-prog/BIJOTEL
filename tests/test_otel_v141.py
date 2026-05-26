"""OTel GenAI semantic conventions v1.41 attribute coverage (v2.4.0).

BIJOTEL's ``canonical_dict`` captures every ``gen_ai.*`` attribute by
default (the include path is a wildcard ``dict(span.attributes)``), so
**capture** is forward-compatible by construction. v2.4.0 specifically
covers the downstream consumers:

- The semantic-dedup exclude list (CAS) now ignores reasoning tokens,
  TTFC, and the singular ``finish_reason`` (per-call output, varies).
- The energy estimator becomes cache-aware: cached reads ≈ 10% energy,
  cache creation at regular input rate, reasoning tokens at regular
  output rate. Old 3-arg `estimate_wh(model, in, out)` calls preserve
  their previous answer (new kwargs default to 0).
- The F12 regression detector exposes three new dimensions
  (``cache_ratio``, ``reasoning_ratio``, ``ttfc_ms``) that return
  None when the attribute is absent — old chains without these
  attributes simply produce no datapoints, never raise.

Critical: every test in this file also asserts **backward
compatibility** — a chain with pre-v2.4 entries (only the legacy
gen_ai.usage.input_tokens / output_tokens) must continue to verify
and produce the same dimension values as v2.3.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.layers.energy import (
    EnergyEstimator,
    EnergyTracker,
)
from bijotel.processors import (
    HmacChainSpanProcessor,
    verify_chain,
    verify_export,
)
from bijotel.processors.canonical import (
    SEMANTIC_EXCLUDE_ATTRS,
    span_to_canonical_dict,
    span_to_semantic_dict,
)
from bijotel.processors.export import export_chain
from bijotel.regression import (
    DimensionStats,
    RegressionDetector,
    compute_baseline,
)
from bijotel.regression.baseline import (
    VALID_DIMENSIONS,
    _extract_dimension_value,
)

SECRET = b"x" * 32


# ──────────────────── canonical_dict (capture) ────────────────────


def test_canonical_dict_captures_v141_attributes_unchanged() -> None:
    """canonical_dict is wildcard — every attribute lands in the dict.

    No "include list" exists to update. Smoke: emit a span with v1.41
    attrs, confirm they appear in the canonical body.
    """
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("v141-capture")
    with tracer.start_as_current_span("span-with-v141") as span:
        span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
        span.set_attribute("gen_ai.usage.input_tokens", 100)
        span.set_attribute("gen_ai.usage.output_tokens", 50)
        # v1.41 new attributes
        span.set_attribute("gen_ai.usage.cache_read.input_tokens", 800)
        span.set_attribute("gen_ai.usage.cache_creation.input_tokens", 200)
        span.set_attribute("gen_ai.usage.reasoning.output_tokens", 30)
        span.set_attribute("gen_ai.response.time_to_first_chunk", 420.5)
        span.set_attribute("gen_ai.agent.version", "v1.2.3")
        span.set_attribute("gen_ai.agent.name", "research-assistant")
        span.set_attribute("gen_ai.request.seed", 42)
        span.set_attribute("gen_ai.response.finish_reason", "stop")

        # Need to read the ReadableSpan inside the context — capture via processor.
        readable = span._readable_span() if hasattr(span, "_readable_span") else span
        d = span_to_canonical_dict(readable)

    attrs = d["attributes"]
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 800
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 200
    assert attrs["gen_ai.usage.reasoning.output_tokens"] == 30
    assert attrs["gen_ai.response.time_to_first_chunk"] == pytest.approx(420.5)
    assert attrs["gen_ai.agent.version"] == "v1.2.3"
    assert attrs["gen_ai.agent.name"] == "research-assistant"
    assert attrs["gen_ai.request.seed"] == 42
    assert attrs["gen_ai.response.finish_reason"] == "stop"
    provider.shutdown()


def test_semantic_exclude_drops_reasoning_and_ttfc() -> None:
    """v1.41 per-call attrs (reasoning tokens, TTFC, finish_reason) are
    excluded from the semantic-dedup body — they vary across runs."""
    assert "gen_ai.usage.reasoning.output_tokens" in SEMANTIC_EXCLUDE_ATTRS
    assert "gen_ai.response.time_to_first_chunk" in SEMANTIC_EXCLUDE_ATTRS
    assert "gen_ai.response.finish_reason" in SEMANTIC_EXCLUDE_ATTRS
    # And the v2.3-and-earlier ones still excluded (regression guard).
    assert "gen_ai.usage.cache_read.input_tokens" in SEMANTIC_EXCLUDE_ATTRS
    assert "gen_ai.usage.cache_creation.input_tokens" in SEMANTIC_EXCLUDE_ATTRS


def test_semantic_dict_excludes_v141_per_call_attrs() -> None:
    """End-to-end: a span with v1.41 attrs produces a semantic body
    that doesn't carry the per-call-varying fields."""
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("v141-semantic")
    with tracer.start_as_current_span("span-sem") as span:
        span.set_attribute("gen_ai.usage.reasoning.output_tokens", 30)
        span.set_attribute("gen_ai.response.time_to_first_chunk", 420.5)
        span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
        readable = span._readable_span() if hasattr(span, "_readable_span") else span
        sem = span_to_semantic_dict(readable)

    sem_attrs = sem["attributes"]
    assert "gen_ai.usage.reasoning.output_tokens" not in sem_attrs
    assert "gen_ai.response.time_to_first_chunk" not in sem_attrs
    # But the request model — which IS input — must remain.
    assert sem_attrs["gen_ai.request.model"] == "claude-haiku-4-5-20251001"
    provider.shutdown()


# ──────────────────── energy: cache-aware estimation ────────────────────


def test_energy_legacy_three_arg_signature_unchanged() -> None:
    """v2.3 callers: ``estimate_wh(model, in, out)`` returns the SAME
    value as before — cache/reasoning defaults are 0."""
    est = EnergyEstimator()
    legacy = est.estimate_wh("claude-haiku-4-5-20251001", 1000, 500)
    # 1500/1000 * 0.0010 = 0.0015 Wh
    assert legacy == pytest.approx(0.0015)


def test_energy_cache_read_reduces_cost() -> None:
    """cached reads count at 0.1x; same total tokens → lower Wh."""
    est = EnergyEstimator()
    no_cache = est.estimate_wh("claude-haiku-4-5-20251001", 1000, 500)
    with_cache = est.estimate_wh(
        "claude-haiku-4-5-20251001",
        tokens_in=200,
        tokens_out=500,
        cache_read_tokens=800,  # 800 of the 1000 "input" came from cache
    )
    # billable = 200 + 0.1*800 + 500 = 780, vs 1500 baseline
    expected = (200 + 0.1 * 800 + 500) / 1000 * 0.0010
    assert with_cache == pytest.approx(expected)
    assert with_cache < no_cache


def test_energy_cache_creation_at_normal_rate() -> None:
    """cache_creation tokens are full input rate (not discounted)."""
    est = EnergyEstimator()
    wh = est.estimate_wh(
        "claude-haiku-4-5-20251001",
        tokens_in=0,
        tokens_out=0,
        cache_creation_tokens=1000,
    )
    # 1000/1000 * 0.0010 = 0.0010
    assert wh == pytest.approx(0.0010)


def test_energy_reasoning_tokens_at_output_rate() -> None:
    """reasoning_output_tokens are 1.0x (same as regular output)."""
    est = EnergyEstimator()
    wh_no_reasoning = est.estimate_wh("claude-haiku-4-5-20251001", 100, 200)
    wh_with_reasoning = est.estimate_wh(
        "claude-haiku-4-5-20251001",
        tokens_in=100,
        tokens_out=200,
        reasoning_output_tokens=300,
    )
    # billable rises by 300 tokens at the same rate.
    assert wh_with_reasoning > wh_no_reasoning
    expected_delta = 300 / 1000 * 0.0010
    assert wh_with_reasoning - wh_no_reasoning == pytest.approx(expected_delta)


def test_energy_negative_v141_values_clamped_to_zero() -> None:
    """Defensive: negative cache/reasoning treated as 0, same as legacy."""
    est = EnergyEstimator()
    wh = est.estimate_wh(
        "claude-haiku-4-5-20251001",
        tokens_in=100,
        tokens_out=50,
        cache_read_tokens=-9999,
        cache_creation_tokens=-9999,
        reasoning_output_tokens=-9999,
    )
    # Should equal pure 100+50 baseline.
    assert wh == pytest.approx(est.estimate_wh("claude-haiku-4-5-20251001", 100, 50))


def test_energy_tracker_record_accepts_v141_kwargs(tmp_path: Path) -> None:
    """EnergyTracker.record() forwards new kwargs to the estimator."""
    db = tmp_path / "energy.db"
    import sqlite3
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE chain (seq INTEGER PRIMARY KEY, timestamp_ns INTEGER)")
    tracker = EnergyTracker(db)
    wh_legacy, _ = tracker.record(
        model="claude-haiku-4-5-20251001",
        tokens_in=1000, tokens_out=500,
    )
    wh_cached, _ = tracker.record(
        model="claude-haiku-4-5-20251001",
        tokens_in=200, tokens_out=500,
        cache_read_tokens=800,
    )
    assert wh_cached < wh_legacy


# ──────────────────── regression: new dimensions ────────────────────


def test_valid_dimensions_includes_v141() -> None:
    assert "cache_ratio" in VALID_DIMENSIONS
    assert "reasoning_ratio" in VALID_DIMENSIONS
    assert "ttfc_ms" in VALID_DIMENSIONS
    # Backward compat: legacy dimensions still in the tuple.
    assert "input_tokens" in VALID_DIMENSIONS
    assert "output_tokens" in VALID_DIMENSIONS
    assert "cost" in VALID_DIMENSIONS


def test_extract_cache_ratio_present() -> None:
    body = {
        "attributes": {
            "gen_ai.usage.input_tokens": 200,
            "gen_ai.usage.cache_read.input_tokens": 800,
        }
    }
    v = _extract_dimension_value(body, "cache_ratio")
    # cache_read / (input + cache_read) = 800 / 1000 = 0.8
    assert v == pytest.approx(0.8)


def test_extract_cache_ratio_absent_returns_none() -> None:
    """Pre-v2.4 chain entries don't have cache_read → None (no datapoint)."""
    body = {"attributes": {"gen_ai.usage.input_tokens": 1000}}
    assert _extract_dimension_value(body, "cache_ratio") is None


def test_extract_reasoning_ratio_present() -> None:
    body = {
        "attributes": {
            "gen_ai.usage.output_tokens": 200,
            "gen_ai.usage.reasoning.output_tokens": 800,
        }
    }
    v = _extract_dimension_value(body, "reasoning_ratio")
    # 800 / (200 + 800) = 0.8
    assert v == pytest.approx(0.8)


def test_extract_reasoning_ratio_absent_returns_none() -> None:
    body = {"attributes": {"gen_ai.usage.output_tokens": 500}}
    assert _extract_dimension_value(body, "reasoning_ratio") is None


def test_extract_ttfc_present() -> None:
    body = {"attributes": {"gen_ai.response.time_to_first_chunk": 420.5}}
    assert _extract_dimension_value(body, "ttfc_ms") == pytest.approx(420.5)


def test_extract_ttfc_absent_returns_none() -> None:
    assert _extract_dimension_value({"attributes": {}}, "ttfc_ms") is None


def test_extract_cache_ratio_zero_denominator_returns_none() -> None:
    """Edge case: both fields present but both zero — no signal."""
    body = {
        "attributes": {
            "gen_ai.usage.input_tokens": 0,
            "gen_ai.usage.cache_read.input_tokens": 0,
        }
    }
    assert _extract_dimension_value(body, "cache_ratio") is None


# ──────────────────── backward compatibility (critical) ────────────────────


@pytest.fixture
def chain_with_mixed_entries(tmp_path: Path) -> Path:
    """Chain with 3 v2.3-shape entries + 3 v2.4-shape (with v1.41 attrs)."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("backward-compat")
    # 3 pre-v2.4 spans (no cache, no reasoning, no TTFC)
    for i in range(3):
        with tracer.start_as_current_span(f"old-span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 100 + i)
            span.set_attribute("gen_ai.usage.output_tokens", 50)
    # 3 v2.4-shape spans (with v1.41 attrs)
    for i in range(3):
        with tracer.start_as_current_span(f"new-span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 100)
            span.set_attribute("gen_ai.usage.output_tokens", 50)
            span.set_attribute("gen_ai.usage.cache_read.input_tokens", 800)
            span.set_attribute("gen_ai.usage.reasoning.output_tokens", 25)
            span.set_attribute("gen_ai.response.time_to_first_chunk", 200.0 + i)
            span.set_attribute("gen_ai.response.finish_reason", "stop")
            span.set_attribute("gen_ai.agent.version", "v1.2.3")
    provider.shutdown()
    return db


def test_mixed_chain_verifies_end_to_end(chain_with_mixed_entries: Path) -> None:
    """6 entries (3 old shape + 3 new shape) verify as one continuous chain."""
    valid, seq, reason = verify_chain(chain_with_mixed_entries, SECRET)
    assert valid is True, reason


def test_mixed_chain_exports_and_verify_export_roundtrips(
    chain_with_mixed_entries: Path, tmp_path: Path
) -> None:
    out = tmp_path / "mixed.json"
    export_chain(chain_with_mixed_entries, out, SECRET)
    valid, reason = verify_export(out, SECRET)
    assert valid is True, reason


def test_old_only_chain_extracts_legacy_dimensions_only(
    chain_with_mixed_entries: Path,
) -> None:
    """Compute baseline on legacy dim → has datapoints. Compute on
    new dim → only counts the 3 v2.4 entries (insufficient = returns
    None per MIN_SAMPLES=5)."""
    stats_legacy = compute_baseline(
        chain_with_mixed_entries, "input_tokens", window=100
    )
    assert isinstance(stats_legacy, DimensionStats)
    assert stats_legacy.sample_count == 6  # all 6 entries

    stats_cache = compute_baseline(
        chain_with_mixed_entries, "cache_ratio", window=100
    )
    # Only 3 entries have cache_ratio data — below MIN_SAMPLES=5 → None.
    assert stats_cache is None


def test_regression_detect_handles_new_dim_with_old_chain(
    chain_with_mixed_entries: Path,
) -> None:
    """Detector on cache_ratio against a chain with only 3 datapoints
    returns gracefully (no datapoints, no anomalies, no crash)."""
    det = RegressionDetector(db_path=chain_with_mixed_entries)
    anomalies = det.detect(dimension="cache_ratio")
    # Below MIN_SAMPLES baseline can't be computed; detect returns [].
    assert anomalies == []


def test_detect_all_dimensions_includes_v141(
    chain_with_mixed_entries: Path,
) -> None:
    """The all-dims sweep covers cache_ratio / reasoning_ratio / ttfc_ms
    AND the three legacy dimensions — 6 total."""
    det = RegressionDetector(db_path=chain_with_mixed_entries)
    results = det.detect_all_dimensions()
    # Six keys expected — legacy + v1.41.
    assert set(results.keys()) == set(VALID_DIMENSIONS)
