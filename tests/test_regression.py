"""Tests for regression detection (F12, Bijuteria #16)."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.cli.main import main as cli_main
from bijotel.processors import HmacChainSpanProcessor
from bijotel.regression import (
    AnomalyMethod,
    DimensionStats,
    RegressionDetector,
    compute_baseline,
)

SECRET = b"x" * 32


def _build_chain(
    db_path: Path,
    *,
    n_baseline: int = 100,
    baseline_input: int = 100,
    baseline_output: int = 50,
    n_evaluation: int = 10,
    eval_input: int = 100,
    eval_output: int = 50,
    model: str = "claude-haiku-4-5-20251001",
) -> Path:
    """Build a chain.db with controllable baseline + evaluation distributions."""
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET)
    )
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")

    # Baseline spans
    for _ in range(n_baseline):
        with tracer.start_as_current_span("anthropic.chat") as s:
            s.set_attribute("gen_ai.request.model", model)
            s.set_attribute("gen_ai.usage.input_tokens", baseline_input)
            s.set_attribute("gen_ai.usage.output_tokens", baseline_output)

    # Evaluation spans
    for _ in range(n_evaluation):
        with tracer.start_as_current_span("anthropic.chat") as s:
            s.set_attribute("gen_ai.request.model", model)
            s.set_attribute("gen_ai.usage.input_tokens", eval_input)
            s.set_attribute("gen_ai.usage.output_tokens", eval_output)

    provider.shutdown()
    return db_path


# ─── Baseline tests ───


def test_compute_baseline_basic(tmp_path: Path) -> None:
    """100 spans with constant tokens → DimensionStats with mean=100, stdev=0."""
    db = tmp_path / "chain.db"
    _build_chain(db, n_baseline=100, baseline_input=100, n_evaluation=0)

    stats = compute_baseline(db, "input_tokens", window=100)
    assert isinstance(stats, DimensionStats)
    assert stats.sample_count == 100
    assert stats.mean == 100.0
    assert stats.stdev == 0.0  # All identical → stdev=0
    assert stats.min_val == 100.0
    assert stats.max_val == 100.0


def test_compute_baseline_insufficient_data_returns_none(tmp_path: Path) -> None:
    """<5 samples → None."""
    db = tmp_path / "chain.db"
    _build_chain(db, n_baseline=3, n_evaluation=0)

    stats = compute_baseline(db, "input_tokens", window=100)
    assert stats is None


def test_compute_baseline_filter_model(tmp_path: Path) -> None:
    """Filter by model — only counts matching model spans."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")

    # 10 Haiku + 5 Sonnet
    for _ in range(10):
        with tracer.start_as_current_span("anthropic.chat") as s:
            s.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
            s.set_attribute("gen_ai.usage.input_tokens", 100)
            s.set_attribute("gen_ai.usage.output_tokens", 50)
    for _ in range(5):
        with tracer.start_as_current_span("anthropic.chat") as s:
            s.set_attribute("gen_ai.request.model", "claude-sonnet-4-20250514")
            s.set_attribute("gen_ai.usage.input_tokens", 200)
            s.set_attribute("gen_ai.usage.output_tokens", 100)
    provider.shutdown()

    haiku_stats = compute_baseline(
        db, "input_tokens", window=100, filter_model="claude-haiku-4-5"
    )
    assert haiku_stats is not None
    assert haiku_stats.sample_count == 10
    assert haiku_stats.mean == 100.0


def test_compute_baseline_each_dimension(tmp_path: Path) -> None:
    """input_tokens, output_tokens, cost — all computable."""
    db = tmp_path / "chain.db"
    _build_chain(
        db, n_baseline=50, baseline_input=1000, baseline_output=500, n_evaluation=0
    )

    in_stats = compute_baseline(db, "input_tokens", window=50)
    out_stats = compute_baseline(db, "output_tokens", window=50)
    cost_stats = compute_baseline(db, "cost", window=50)

    assert in_stats is not None and in_stats.mean == 1000.0
    assert out_stats is not None and out_stats.mean == 500.0
    # Cost: 1000 * 0.0008 + 500 * 0.0040 = 0.8 + 2.0 = 2.8 / 1000 = 0.0028
    assert cost_stats is not None
    assert abs(cost_stats.mean - 0.0028) < 1e-6


def test_compute_baseline_window_truncates(tmp_path: Path) -> None:
    """window=10 caps sample_count regardless of total spans."""
    db = tmp_path / "chain.db"
    _build_chain(db, n_baseline=50, n_evaluation=0)

    stats = compute_baseline(db, "input_tokens", window=10)
    assert stats is not None
    assert stats.sample_count == 10


def test_compute_baseline_invalid_dimension_raises(tmp_path: Path) -> None:
    """Unknown dimension raises ValueError."""
    db = tmp_path / "chain.db"
    _build_chain(db, n_baseline=10, n_evaluation=0)

    with pytest.raises(ValueError, match="dimension must be one of"):
        compute_baseline(db, "unknown_metric", window=10)


# ─── Detector tests ───


def test_detector_no_anomalies_normal_data(tmp_path: Path) -> None:
    """Identical data baseline + evaluation → zero anomalies."""
    db = tmp_path / "chain.db"
    _build_chain(
        db,
        n_baseline=100,
        baseline_input=100,
        baseline_output=50,
        n_evaluation=10,
        eval_input=100,
        eval_output=50,
    )

    detector = RegressionDetector(db, baseline_window=100, z_threshold=3.0)
    anomalies = detector.detect("input_tokens")
    # All values identical → stdev=0 → z-score path skipped → no anomaly via BOTH
    assert anomalies == []


def test_detector_iqr_triggers_extreme_value(tmp_path: Path) -> None:
    """Mix baseline 100±10 with one extreme outlier → IQR catches it."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")

    # 100 baseline spans with values 90-110 (varied for non-zero stdev/iqr)
    import random
    random.seed(42)
    for _ in range(100):
        with tracer.start_as_current_span("anthropic.chat") as s:
            s.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
            s.set_attribute("gen_ai.usage.input_tokens", random.randint(90, 110))
            s.set_attribute("gen_ai.usage.output_tokens", 50)

    # 5 evaluation spans, last one extreme (10x baseline)
    for _i in range(4):
        with tracer.start_as_current_span("anthropic.chat") as s:
            s.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
            s.set_attribute("gen_ai.usage.input_tokens", 100)
            s.set_attribute("gen_ai.usage.output_tokens", 50)
    # Extreme outlier
    with tracer.start_as_current_span("anthropic.chat") as s:
        s.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        s.set_attribute("gen_ai.usage.input_tokens", 1000)
        s.set_attribute("gen_ai.usage.output_tokens", 50)

    provider.shutdown()

    detector = RegressionDetector(
        db, baseline_window=100, z_threshold=3.0, method=AnomalyMethod.BOTH
    )
    anomalies = detector.detect("input_tokens", since_seq=101)
    assert len(anomalies) >= 1
    # The last one should be the extreme outlier
    extreme = [a for a in anomalies if a.value == 1000.0]
    assert len(extreme) == 1


def test_detector_method_z_score_only(tmp_path: Path) -> None:
    """method=Z_SCORE flags based on z alone."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")

    import random
    random.seed(123)
    for _ in range(100):
        with tracer.start_as_current_span("anthropic.chat") as s:
            s.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
            s.set_attribute("gen_ai.usage.input_tokens", random.randint(95, 105))
            s.set_attribute("gen_ai.usage.output_tokens", 50)

    # One outlier 5x baseline
    with tracer.start_as_current_span("anthropic.chat") as s:
        s.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        s.set_attribute("gen_ai.usage.input_tokens", 500)
        s.set_attribute("gen_ai.usage.output_tokens", 50)

    provider.shutdown()

    detector = RegressionDetector(
        db, baseline_window=100, z_threshold=3.0, method=AnomalyMethod.Z_SCORE
    )
    anomalies = detector.detect("input_tokens", since_seq=101)
    assert len(anomalies) == 1
    assert anomalies[0].method_triggered == "z_score"
    assert anomalies[0].z_score is not None
    assert anomalies[0].z_score > 3.0


def test_detector_invalid_dimension_raises(tmp_path: Path) -> None:
    """Detector raises ValueError on bad dimension."""
    db = tmp_path / "chain.db"
    _build_chain(db, n_baseline=10, n_evaluation=2)

    detector = RegressionDetector(db)
    with pytest.raises(ValueError, match="dimension must be one of"):
        detector.detect("bogus_dim")


def test_detect_all_dimensions_returns_dict(tmp_path: Path) -> None:
    """detect_all_dimensions returns dict keyed by dimension."""
    db = tmp_path / "chain.db"
    _build_chain(db, n_baseline=50, n_evaluation=5)

    detector = RegressionDetector(db)
    results = detector.detect_all_dimensions()

    assert set(results.keys()) == {"input_tokens", "output_tokens", "cost"}
    for v in results.values():
        assert isinstance(v, list)


def test_detector_empty_chain_returns_empty(tmp_path: Path) -> None:
    """No spans → empty anomaly list (no crash)."""
    db = tmp_path / "chain.db"
    HmacChainSpanProcessor(db_path=db, secret_key=SECRET)  # Init schema only

    detector = RegressionDetector(db)
    anomalies = detector.detect("input_tokens")
    assert anomalies == []


def test_detector_insufficient_baseline(tmp_path: Path) -> None:
    """<5 baseline spans → empty list (cannot compute baseline)."""
    db = tmp_path / "chain.db"
    _build_chain(db, n_baseline=3, n_evaluation=2)

    detector = RegressionDetector(db)
    anomalies = detector.detect("input_tokens", since_seq=4)
    assert anomalies == []


# ─── CLI tests ───


def test_regression_cmd_no_anomalies_exit_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No anomalies → CLI exits 0."""
    db = tmp_path / "chain.db"
    _build_chain(db, n_baseline=100, n_evaluation=10)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(["regression", "--db", str(db)])
    assert rc == 0
    assert "Total anomalies: 0" in out.getvalue()


def test_regression_cmd_with_anomalies_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anomalies present → CLI exits 1."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")

    import random
    random.seed(7)
    for _ in range(100):
        with tracer.start_as_current_span("anthropic.chat") as s:
            s.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
            s.set_attribute("gen_ai.usage.input_tokens", random.randint(95, 105))
            s.set_attribute("gen_ai.usage.output_tokens", 50)
    # Extreme outlier
    with tracer.start_as_current_span("anthropic.chat") as s:
        s.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        s.set_attribute("gen_ai.usage.input_tokens", 5000)
        s.set_attribute("gen_ai.usage.output_tokens", 50)

    provider.shutdown()

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(
            [
                "regression",
                "--db",
                str(db),
                "--dimension",
                "input_tokens",
            ]
        )
    assert rc == 1


def test_regression_cmd_missing_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nonexistent db → exit 2 + error in stderr."""
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(["regression", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "not found" in err.getvalue().lower()


def test_regression_cmd_specific_dimension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dimension cost → only runs detection on cost."""
    db = tmp_path / "chain.db"
    _build_chain(db, n_baseline=100, n_evaluation=5)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(
            ["regression", "--db", str(db), "--dimension", "cost"]
        )
    assert rc in (0, 1)
    output = out.getvalue()
    assert "[cost]" in output
    # Other dimensions should NOT appear in output
    assert "[input_tokens]" not in output
    assert "[output_tokens]" not in output
