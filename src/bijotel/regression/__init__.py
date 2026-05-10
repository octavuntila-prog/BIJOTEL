"""Regression Detection — Bijuteria #16 (F12).

Temporal anomaly detection over BIJOTEL chain.db using z-score + IQR
methods on universal dimensions (input_tokens, output_tokens, cost).
"""

from bijotel.regression.baseline import DimensionStats, compute_baseline
from bijotel.regression.detector import (
    Anomaly,
    AnomalyMethod,
    RegressionDetector,
)

__all__ = [
    "Anomaly",
    "AnomalyMethod",
    "DimensionStats",
    "RegressionDetector",
    "compute_baseline",
]
