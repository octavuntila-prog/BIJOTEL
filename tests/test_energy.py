"""Tests for :mod:`bijotel.layers.energy` (Bijuteria #3 — v1.9.0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bijotel.layers.energy import (
    DEFAULT_ENERGY_RATES,
    DEFAULT_RATE_FALLBACK,
    AgentEnergy,
    CarbonCalculator,
    EnergyEstimator,
    EnergySpanProcessor,
    EnergyTracker,
    ModelEnergy,
    energy_budget,
)
from bijotel.policy.decision import State

# ============================================================================
# EnergyEstimator
# ============================================================================


def test_estimate_haiku_1k_tokens() -> None:
    est = EnergyEstimator()
    # 1000 tokens × 0.001 Wh/1K = 0.001 Wh
    assert est.estimate_wh("claude-haiku-4-5", 500, 500) == pytest.approx(0.001)


def test_estimate_sonnet_1k_tokens() -> None:
    est = EnergyEstimator()
    # 1000 tokens × 0.003 Wh/1K = 0.003 Wh
    assert est.estimate_wh("claude-sonnet-4", 500, 500) == pytest.approx(0.003)


def test_estimate_opus_more_expensive_than_haiku() -> None:
    est = EnergyEstimator()
    haiku = est.estimate_wh("claude-haiku-4-5", 1000, 0)
    opus = est.estimate_wh("claude-opus-4", 1000, 0)
    assert opus > haiku
    # Opus rate is 10x haiku (0.01 vs 0.001)
    assert opus == pytest.approx(haiku * 10)


def test_estimate_unknown_model_uses_fallback() -> None:
    est = EnergyEstimator()
    wh = est.estimate_wh("definitely-not-a-real-model", 1000, 0)
    assert wh == pytest.approx(DEFAULT_RATE_FALLBACK)


def test_estimate_zero_tokens() -> None:
    est = EnergyEstimator()
    assert est.estimate_wh("claude-haiku-4-5", 0, 0) == 0.0


def test_estimate_negative_tokens_treated_as_zero() -> None:
    """Defensive: negative token counts (instrumentation glitch) → 0, not negative Wh."""
    est = EnergyEstimator()
    assert est.estimate_wh("claude-haiku-4-5", -100, -50) == 0.0


def test_estimate_custom_rates_override() -> None:
    est = EnergyEstimator(rates={"my-model": 0.05})
    # 1K tokens × 0.05 = 0.05 Wh
    assert est.estimate_wh("my-model", 1000, 0) == pytest.approx(0.05)


def test_estimate_rejects_zero_fallback() -> None:
    with pytest.raises(ValueError, match="fallback"):
        EnergyEstimator(fallback=0)
    with pytest.raises(ValueError, match="fallback"):
        EnergyEstimator(fallback=-1)


def test_estimate_rates_property_returns_copy() -> None:
    est = EnergyEstimator()
    r = est.rates
    r["claude-haiku-4-5"] = 99999.0
    # Internal state unchanged
    assert est.rates["claude-haiku-4-5"] == DEFAULT_ENERGY_RATES["claude-haiku-4-5"]


# ============================================================================
# CarbonCalculator
# ============================================================================


def test_co2_us_east_default() -> None:
    calc = CarbonCalculator("us-east")
    # 1000 Wh = 1 kWh × 380 g/kWh = 380 g
    assert calc.wh_to_co2_grams(1000) == pytest.approx(380.0)


def test_co2_eu_north_very_low() -> None:
    """Sweden/Norway hydro+nuclear → 30 g/kWh."""
    calc = CarbonCalculator("eu-north")
    assert calc.intensity_g_per_kwh == 30.0
    assert calc.wh_to_co2_grams(1000) == pytest.approx(30.0)


def test_co2_unknown_region_falls_back_to_world() -> None:
    calc = CarbonCalculator("mars-colony-3")
    assert calc.region == "world"
    assert calc.intensity_g_per_kwh == 450.0


def test_co2_zero_wh() -> None:
    calc = CarbonCalculator()
    assert calc.wh_to_co2_grams(0) == 0.0


def test_co2_negative_wh_is_zero() -> None:
    calc = CarbonCalculator()
    assert calc.wh_to_co2_grams(-10) == 0.0


def test_co2_intensity_override() -> None:
    calc = CarbonCalculator(
        "datacenter-x",
        intensity_overrides={"datacenter-x": 60.0},
    )
    assert calc.region == "datacenter-x"
    assert calc.intensity_g_per_kwh == 60.0


# ============================================================================
# EnergyTracker — SQLite-backed
# ============================================================================


def test_tracker_record_and_summary(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "energy.db")
    wh, co2 = tracker.record(
        "claude-haiku-4-5-20251001", 800, 500,
        timestamp_ns=1779609753000000000,
        agent_id="v3-atelier",
    )
    assert wh > 0
    assert co2 > 0
    s = tracker.summary()
    assert s.total_calls == 1
    assert s.total_tokens == 1300
    assert s.total_wh == pytest.approx(wh)
    assert s.total_co2_grams == pytest.approx(co2)


def test_tracker_summary_per_model(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "energy.db")
    tracker.record("claude-haiku-4-5", 1000, 500)
    tracker.record("claude-haiku-4-5", 800, 400)
    tracker.record("claude-sonnet-4", 1000, 500)
    s = tracker.summary()
    assert "claude-haiku-4-5" in s.per_model
    assert "claude-sonnet-4" in s.per_model
    haiku_e: ModelEnergy = s.per_model["claude-haiku-4-5"]
    assert haiku_e.calls == 2
    sonnet_e: ModelEnergy = s.per_model["claude-sonnet-4"]
    assert sonnet_e.calls == 1
    # Sonnet uses more energy per token → higher wh despite same token count
    assert sonnet_e.wh > haiku_e.wh / 2  # rough — 1 sonnet call vs 2 haiku calls


def test_tracker_summary_per_agent(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "energy.db")
    tracker.record("claude-haiku-4-5", 1000, 500, agent_id="v3")
    tracker.record("claude-haiku-4-5", 1000, 500, agent_id="v4")
    tracker.record("claude-haiku-4-5", 1000, 500, agent_id="v3")
    s = tracker.summary()
    v3: AgentEnergy = s.per_agent["v3"]
    v4: AgentEnergy = s.per_agent["v4"]
    assert v3.calls == 2
    assert v4.calls == 1


def test_tracker_summary_since_filter(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "energy.db")
    tracker.record("claude-haiku-4-5", 1000, 0, timestamp_ns=1000)
    tracker.record("claude-haiku-4-5", 1000, 0, timestamp_ns=2000)
    tracker.record("claude-haiku-4-5", 1000, 0, timestamp_ns=3000)
    s = tracker.summary(since=2000)
    assert s.total_calls == 2  # rows at 2000 and 3000


def test_tracker_summary_until_filter(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "energy.db")
    tracker.record("claude-haiku-4-5", 1000, 0, timestamp_ns=1000)
    tracker.record("claude-haiku-4-5", 1000, 0, timestamp_ns=2000)
    tracker.record("claude-haiku-4-5", 1000, 0, timestamp_ns=3000)
    s = tracker.summary(until=3000)
    assert s.total_calls == 2  # rows at 1000 and 2000 (3000 excluded)


def test_tracker_summary_agent_filter(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "energy.db")
    tracker.record("claude-haiku-4-5", 1000, 0, agent_id="v3")
    tracker.record("claude-haiku-4-5", 1000, 0, agent_id="v4")
    s = tracker.summary(agent_id="v3")
    assert s.total_calls == 1


def test_tracker_equivalents_calculated(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "energy.db")
    # 1M tokens haiku = 1 Wh; 1 Wh × 380 / 1000 = 0.38 g CO2
    # Equivalents: 0.38/120 km, 1/10 phone charges, 1/100 kettle boils
    for _ in range(100):
        tracker.record("claude-haiku-4-5", 5000, 5000)  # 100 × 10K = 1M tokens
    s = tracker.summary()
    assert s.total_wh == pytest.approx(1.0, rel=0.01)
    assert s.equivalent_km_driven > 0
    assert s.equivalent_phone_charges == pytest.approx(0.1, rel=0.01)
    assert s.equivalent_kettle_boils == pytest.approx(0.01, rel=0.01)


def test_tracker_record_idempotent_on_span_seq(tmp_path: Path) -> None:
    """Passing the same span_seq twice INSERTs once (backfill safety)."""
    tracker = EnergyTracker(tmp_path / "energy.db")
    tracker.record("claude-haiku-4-5", 1000, 500, span_seq=42)
    tracker.record("claude-haiku-4-5", 1000, 500, span_seq=42)  # dup
    tracker.record("claude-haiku-4-5", 1000, 500, span_seq=43)  # new
    s = tracker.summary()
    assert s.total_calls == 2


def test_tracker_spent_today(tmp_path: Path) -> None:
    """spent_today_wh sums today's entries for the given agent."""
    import datetime as _dt
    tracker = EnergyTracker(tmp_path / "energy.db")
    # Today entries
    tracker.record("claude-haiku-4-5", 1000, 500, agent_id="v3")
    tracker.record("claude-haiku-4-5", 2000, 1000, agent_id="v3")
    # Yesterday's entry — shouldn't count
    yesterday_ns = int(
        (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=2)).timestamp() * 1e9
    )
    tracker.record(
        "claude-haiku-4-5", 5000, 5000,
        timestamp_ns=yesterday_ns, agent_id="v3",
    )
    today = tracker.spent_today_wh(agent_id="v3")
    # Today should be ~ 3.5K tokens worth at haiku rate (1.5K + 3K = 4.5K)
    # wait: 1500 + 3000 = 4500 tokens × 0.001/1K = 0.0045 Wh
    assert today == pytest.approx(0.0045, rel=0.05)


def test_tracker_iso_string_timestamps(tmp_path: Path) -> None:
    """Summary accepts ISO-8601 string for since/until."""
    tracker = EnergyTracker(tmp_path / "energy.db")
    tracker.record("claude-haiku-4-5", 1000, 0, timestamp_ns=int(
        __import__("datetime").datetime(2026, 5, 24, 0, 0, 0,
                                         tzinfo=__import__("datetime").UTC).timestamp() * 1e9
    ))
    s = tracker.summary(since="2026-05-24T00:00:00Z")
    assert s.total_calls == 1
    assert s.since is not None
    assert "2026-05-24" in s.since


# ============================================================================
# EnergySpanProcessor — record from spans
# ============================================================================


class _FakeSpan:
    """Minimal stand-in for opentelemetry.sdk.trace.ReadableSpan."""

    def __init__(self, attrs: dict, end_time: int = 1000):
        self.attributes = attrs
        self.end_time = end_time


def test_processor_records_from_span(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "e.db")
    proc = EnergySpanProcessor(tracker)
    proc.on_end(_FakeSpan({
        "gen_ai.request.model": "claude-haiku-4-5-20251001",
        "gen_ai.usage.input_tokens": 500,
        "gen_ai.usage.output_tokens": 200,
        "agent.name": "v3-atelier",
    }))
    s = tracker.summary()
    assert s.total_calls == 1
    assert s.per_agent["v3-atelier"].calls == 1


def test_processor_skips_non_genai_spans(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "e.db")
    proc = EnergySpanProcessor(tracker)
    proc.on_end(_FakeSpan({"span.name": "http.request"}))  # not a GenAI span
    s = tracker.summary()
    assert s.total_calls == 0


def test_processor_skips_zero_tokens(tmp_path: Path) -> None:
    """Spans with model but no usage data are ignored."""
    tracker = EnergyTracker(tmp_path / "e.db")
    proc = EnergySpanProcessor(tracker)
    proc.on_end(_FakeSpan({"gen_ai.request.model": "claude-haiku-4-5"}))
    s = tracker.summary()
    assert s.total_calls == 0


def test_processor_crash_isolated(tmp_path: Path) -> None:
    """A broken span doesn't propagate up."""
    tracker = EnergyTracker(tmp_path / "e.db")
    proc = EnergySpanProcessor(tracker)

    class _BrokenSpan:
        @property
        def attributes(self):
            raise RuntimeError("span corruption")
        end_time = 1000

    # Must NOT raise
    proc.on_end(_BrokenSpan())
    # And nothing was written
    s = tracker.summary()
    assert s.total_calls == 0


def test_processor_protocol_methods_dont_crash() -> None:
    """on_start / shutdown / force_flush are protocol-compliant no-ops."""
    tracker = EnergyTracker.__new__(EnergyTracker)  # skip __init__
    tracker._tracker = None  # unused
    proc = EnergySpanProcessor.__new__(EnergySpanProcessor)
    proc._tracker = tracker
    proc._agent_attr = "agent.name"
    # No-ops shouldn't raise
    proc.on_start(None, None)
    proc.shutdown()
    assert proc.force_flush() is True


# ============================================================================
# energy_budget policy rule
# ============================================================================


def test_energy_budget_allows_under_limit(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "e.db")
    # Small usage today
    tracker.record("claude-haiku-4-5", 1000, 500, agent_id="v3")
    rule = energy_budget(tracker=tracker, daily_limit_wh=10.0, agent_id="v3")
    d = rule({"messages": [{"role": "user", "content": "hi"}]})
    assert d.state == State.ALLOW


def test_energy_budget_warns_over_limit(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "e.db")
    # Burn through budget: 10M tokens of opus ≈ 100 Wh
    for _ in range(100):
        tracker.record("claude-opus-4", 50_000, 50_000, agent_id="v3")
    rule = energy_budget(tracker=tracker, daily_limit_wh=10.0, agent_id="v3", mode="warn")
    d = rule({"messages": [{"role": "user", "content": "hi"}]})
    assert d.state == State.WARN
    assert d.rule == "energy_budget"
    assert "exceeded" in d.reason.lower()


def test_energy_budget_deny_mode(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "e.db")
    for _ in range(100):
        tracker.record("claude-opus-4", 50_000, 50_000, agent_id="v3")
    rule = energy_budget(tracker=tracker, daily_limit_wh=10.0, agent_id="v3", mode="deny")
    d = rule({"messages": [{"role": "user", "content": "hi"}]})
    assert d.state == State.DENY


def test_energy_budget_per_agent_isolation(tmp_path: Path) -> None:
    """Agent A's burn doesn't trip Agent B's budget."""
    tracker = EnergyTracker(tmp_path / "e.db")
    for _ in range(100):
        tracker.record("claude-opus-4", 50_000, 50_000, agent_id="v3")
    rule_v4 = energy_budget(tracker=tracker, daily_limit_wh=10.0, agent_id="v4")
    d = rule_v4({"messages": [{"role": "user", "content": "hi"}]})
    assert d.state == State.ALLOW  # v4 hasn't spent anything


def test_energy_budget_rejects_bad_mode(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "e.db")
    with pytest.raises(ValueError, match="mode"):
        energy_budget(tracker=tracker, daily_limit_wh=10.0, mode="weird")


def test_energy_budget_rejects_zero_limit(tmp_path: Path) -> None:
    tracker = EnergyTracker(tmp_path / "e.db")
    with pytest.raises(ValueError, match="daily_limit"):
        energy_budget(tracker=tracker, daily_limit_wh=0)
    with pytest.raises(ValueError, match="daily_limit"):
        energy_budget(tracker=tracker, daily_limit_wh=-5)


def test_energy_budget_plugs_into_policy_engine(tmp_path: Path) -> None:
    from bijotel.policy import PolicyEngine

    tracker = EnergyTracker(tmp_path / "e.db")
    engine = PolicyEngine(
        rules=[energy_budget(tracker=tracker, daily_limit_wh=10.0, mode="warn")]
    )
    # Under budget — passes
    d, w = engine.evaluate({"messages": [{"role": "user", "content": "hi"}]})
    assert d.state == State.ALLOW
    assert w == []


# ============================================================================
# Integration: full GENA-scale estimate
# ============================================================================


def test_gena_workload_estimate(tmp_path: Path) -> None:
    """Simulate 14 days of GENA traffic: ~390 calls/day, ~1300 tokens avg, Haiku."""
    tracker = EnergyTracker(tmp_path / "e.db")
    for _ in range(5430):
        tracker.record(
            "claude-haiku-4-5-20251001",
            900,  # average input
            400,  # average output
            agent_id="v3-atelier",
        )
    s = tracker.summary()
    assert s.total_calls == 5430
    # 5430 × 1300 / 1000 × 0.001 = 7.06 Wh
    assert s.total_wh == pytest.approx(7.06, rel=0.02)
    # 7.06 Wh × 380/1000 = 2.68 grams CO2 (us-east default)
    assert s.total_co2_grams == pytest.approx(2.68, rel=0.05)
    # < 0.03 km driven equivalent — basically nothing
    assert s.equivalent_km_driven < 0.05
