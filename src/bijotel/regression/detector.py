"""Anomaly detection via z-score + IQR thresholds (F12, Bijuteria #16).

Two complementary methods on the same baseline:

- **z-score** (parametric): ``z = (value - mean) / stdev``. Detects
  far-from-mean values assuming roughly normal distribution. Fast for
  Gaussian-like signals (most token counts).
- **IQR** (non-parametric): tukey-style outlier ``value < p25 - k*iqr``
  or ``value > p75 + k*iqr``. Robust to heavy-tailed distributions
  (cost can be heavy-tailed when one big call dominates).

`AnomalyMethod.BOTH` requires BOTH methods to flag — minimizes false
positives. Use individual methods if you want broader detection.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from bijotel.regression.baseline import (
    VALID_DIMENSIONS,
    DimensionStats,
    _extract_dimension_value,
    compute_baseline,
)


class AnomalyMethod(Enum):
    """Detection method selector."""

    Z_SCORE = "z_score"
    IQR = "iqr"
    BOTH = "both"


@dataclass(frozen=True)
class Anomaly:
    """Single anomaly detection record.

    Attributes:
        seq: chain entry seq where anomaly was detected.
        timestamp: ISO8601 UTC string of the span.
        dimension: which dimension flagged ("input_tokens", "output_tokens", "cost").
        value: observed value.
        baseline_mean: mean of baseline window (for context).
        z_score: z-score against baseline. ``None`` if stdev=0.
        iqr_distance: how many IQRs outside [p25, p75]. Negative = below
            p25, positive = above p75. ``None`` if iqr=0.
        method_triggered: which method(s) flagged ("z_score" / "iqr" / "both").
        severity: "warning" if single method (BOTH-mode mismatched) /
            "anomaly" if both methods triggered.
    """

    seq: int
    timestamp: str
    dimension: str
    value: float
    baseline_mean: float
    z_score: float | None
    iqr_distance: float | None
    method_triggered: str
    severity: str


class RegressionDetector:
    """Detect drift in dimensional metrics over time."""

    DIMENSIONS = VALID_DIMENSIONS

    def __init__(
        self,
        db_path: str | Path,
        *,
        baseline_window: int = 100,
        z_threshold: float = 3.0,
        iqr_multiplier: float = 1.5,
        method: AnomalyMethod = AnomalyMethod.BOTH,
    ) -> None:
        self.db_path = Path(db_path)
        self.baseline_window = baseline_window
        self.z_threshold = z_threshold
        self.iqr_multiplier = iqr_multiplier
        self.method = method

    def detect(
        self,
        dimension: str,
        *,
        filter_model: str | None = None,
        since_seq: int | None = None,
    ) -> list[Anomaly]:
        """Detect anomalies for a given dimension.

        Compares spans (seq >= ``since_seq``) against a baseline computed
        from spans BEFORE ``since_seq``. If ``since_seq`` is None, uses
        the latest 50 spans as evaluation set and the prior window as
        baseline.

        Args:
            dimension: One of ``DIMENSIONS``.
            filter_model: Optional model name filter applied to BOTH
                baseline and evaluation set.
            since_seq: Lowest seq to evaluate (inclusive). Default: top 50.

        Returns:
            List of ``Anomaly`` records, empty if no drift detected.

        Raises:
            ValueError: dimension not in VALID_DIMENSIONS.
        """
        if dimension not in VALID_DIMENSIONS:
            raise ValueError(
                f"dimension must be one of {VALID_DIMENSIONS}, "
                f"got {dimension!r}"
            )

        # Determine evaluation set boundary
        if since_seq is None:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT MAX(seq) FROM chain"
                ).fetchone()
                max_seq = row[0] if row[0] is not None else 0
            since_seq = max(1, max_seq - 49)  # Last 50 spans by default

        # Compute baseline from spans BEFORE since_seq
        baseline = compute_baseline(
            self.db_path,
            dimension=dimension,
            window=self.baseline_window,
            filter_model=filter_model,
            upper_seq=since_seq,
        )
        if baseline is None:
            # Insufficient baseline data — cannot detect anomalies
            return []

        # Iterate evaluation set, score each span
        anomalies: list[Anomaly] = []
        sql = (
            "SELECT seq, timestamp_ns, canonical_body "
            "FROM chain WHERE seq >= ? ORDER BY seq"
        )

        with sqlite3.connect(self.db_path) as conn:
            for seq, ts_ns, body_blob in conn.execute(sql, (since_seq,)):
                try:
                    body = json.loads(
                        body_blob.decode("utf-8")
                        if isinstance(body_blob, bytes)
                        else body_blob
                    )
                except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
                    continue

                if filter_model is not None and body.get(
                    "attributes", {}
                ).get("gen_ai.request.model") != filter_model:
                    continue

                value = _extract_dimension_value(body, dimension)
                if value is None:
                    continue

                anomaly = self._score(seq, ts_ns, dimension, value, baseline)
                if anomaly is not None:
                    anomalies.append(anomaly)

        return anomalies

    def _score(
        self,
        seq: int,
        timestamp_ns: int,
        dimension: str,
        value: float,
        baseline: DimensionStats,
    ) -> Anomaly | None:
        """Score a single value against baseline. Returns Anomaly if flagged."""
        # z-score
        z = None
        z_triggered = False
        if baseline.stdev > 0:
            z = (value - baseline.mean) / baseline.stdev
            z_triggered = abs(z) >= self.z_threshold

        # IQR
        iqr_dist = None
        iqr_triggered = False
        if baseline.iqr > 0:
            # Tukey-style: outside [p25 - k*iqr, p75 + k*iqr]
            lower = baseline.p25 - self.iqr_multiplier * baseline.iqr
            upper = baseline.p75 + self.iqr_multiplier * baseline.iqr
            if value < lower or value > upper:
                iqr_dist = (value - baseline.p50) / baseline.iqr
                iqr_triggered = True

        # Apply method selector
        triggered = False
        method_str = ""
        if self.method == AnomalyMethod.Z_SCORE:
            triggered = z_triggered
            method_str = "z_score"
        elif self.method == AnomalyMethod.IQR:
            triggered = iqr_triggered
            method_str = "iqr"
        elif self.method == AnomalyMethod.BOTH:
            triggered = z_triggered and iqr_triggered
            method_str = "both"

        if not triggered:
            return None

        # Severity: "anomaly" if both methods agree (regardless of selector),
        # "warning" if only one of two methods agrees
        severity = "anomaly" if (z_triggered and iqr_triggered) else "warning"

        ts_str = datetime.datetime.fromtimestamp(
            timestamp_ns / 1e9, tz=datetime.UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        return Anomaly(
            seq=seq,
            timestamp=ts_str,
            dimension=dimension,
            value=value,
            baseline_mean=baseline.mean,
            z_score=z,
            iqr_distance=iqr_dist,
            method_triggered=method_str,
            severity=severity,
        )

    def detect_all_dimensions(
        self,
        *,
        filter_model: str | None = None,
        since_seq: int | None = None,
    ) -> dict[str, list[Anomaly]]:
        """Run detection on all 3 dimensions; returns dict keyed by dimension."""
        return {
            dim: self.detect(
                dim, filter_model=filter_model, since_seq=since_seq
            )
            for dim in self.DIMENSIONS
        }
