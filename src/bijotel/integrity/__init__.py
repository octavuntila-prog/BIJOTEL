"""``bijotel.integrity`` — chain-level anomaly detection (v2.8.0).

Where F12 (``bijotel.regression``) detects drift in the *payload* —
token counts, cost, latency, anything that lives inside a span's
attributes — this module monitors the **chain itself**: sequence gaps,
backward timestamps, hash duplicates, provider-mix shifts, rate
changes, rotation boundaries.

Two different layers of observation:

    F12         : "the model is degrading" (payload drift)
    integrity   : "the chain is behaving abnormally" (structural)

The two are orthogonal — both can be active at the same time and they
look at different rows of evidence. F12 reads span attributes;
integrity reads the chain's own metadata (seq, timestamp_ns,
canonical_hash, prev_hash).

Public API:
    ``ChainIntegrityMonitor`` — the analyzer.
    ``IntegrityReport``       — frozen result with ``.clean`` /
                                ``.anomaly_count``.
    ``analyze_chain_integrity(db_path, *, window=100)`` — one-shot
                                convenience wrapper around the monitor.

Plus the six small dataclasses one per anomaly category:
``SequenceGap``, ``TimestampAnomaly``, ``HashAnomaly``,
``ProviderShift``, ``RateAnomaly``, ``RotationBoundary``.
"""

from __future__ import annotations

from bijotel.integrity.monitor import (
    ChainIntegrityMonitor,
    HashAnomaly,
    IntegrityReport,
    ProviderShift,
    RateAnomaly,
    RotationBoundary,
    SequenceGap,
    TimestampAnomaly,
    analyze_chain_integrity,
)

__all__ = [
    "ChainIntegrityMonitor",
    "HashAnomaly",
    "IntegrityReport",
    "ProviderShift",
    "RateAnomaly",
    "RotationBoundary",
    "SequenceGap",
    "TimestampAnomaly",
    "analyze_chain_integrity",
]
