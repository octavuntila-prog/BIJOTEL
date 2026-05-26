"""``bijotel regression`` — F12 drift detection on chain.db."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bijotel.regression import Anomaly, RegressionDetector


def _print_anomalies(dimension: str, anomalies: list[Anomaly]) -> None:
    """Print anomalies tabular for one dimension."""
    if not anomalies:
        print(f"[{dimension}] no anomalies detected")
        return

    print(f"\n[{dimension}] {len(anomalies)} anomalies:")
    print(
        f"  {'seq':<6} {'timestamp':<22} {'value':<14} {'baseline':<14} "
        f"{'z-score':<10} {'iqr-dist':<10} {'severity':<10}"
    )
    print("  " + "-" * 90)
    for a in anomalies:
        z_str = f"{a.z_score:+.2f}" if a.z_score is not None else "N/A"
        iqr_str = f"{a.iqr_distance:+.2f}" if a.iqr_distance is not None else "N/A"
        print(
            f"  {a.seq:<6} {a.timestamp:<22} {a.value:<14.4f} "
            f"{a.baseline_mean:<14.4f} {z_str:<10} {iqr_str:<10} {a.severity:<10}"
        )


def regression_cmd(args: argparse.Namespace) -> int:
    """Run regression detection on chain.db.

    Returns:
        0 if no anomalies detected
        1 if any anomalies detected
        2 if invalid arguments / chain.db unreadable
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: chain DB not found: {db_path}", file=sys.stderr)
        return 2

    detector = RegressionDetector(
        db_path=db_path,
        baseline_window=args.window,
        z_threshold=args.z_threshold,
    )

    total_anomalies = 0
    if args.dimension:
        # Single dimension
        try:
            anomalies = detector.detect(
                dimension=args.dimension,
                filter_model=args.model,
            )
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        _print_anomalies(args.dimension, anomalies)
        total_anomalies = len(anomalies)
    else:
        # All dimensions
        results = detector.detect_all_dimensions(filter_model=args.model)
        for dim, anomalies in results.items():
            _print_anomalies(dim, anomalies)
            total_anomalies += len(anomalies)

    print(f"\nTotal anomalies: {total_anomalies}")
    return 0 if total_anomalies == 0 else 1
