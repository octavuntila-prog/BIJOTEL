"""``bijotel integrity`` — continuous chain-integrity monitor (v2.8.0).

Reads the last N entries (default 100), runs every detector in
``bijotel.integrity.monitor``, and prints either a tabular human
summary or, with ``--json``, a machine-readable report.

Designed to run from cron — exit code 1 when any anomaly is detected
so a monitoring loop can alert without parsing JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bijotel.integrity import analyze_chain_integrity


def integrity_cmd(args: argparse.Namespace) -> int:
    """Run integrity analysis on a chain DB.

    Exit codes:
        0  — chain is clean (no anomalies).
        1  — anomalies detected (use ``--json`` for details).
        2  — DB missing or argument error.
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: chain DB not found: {db_path}", file=sys.stderr)
        return 2

    try:
        report = analyze_chain_integrity(db_path, window=args.window)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.json:
        # Print one compact JSON line for log-friendly cron output.
        print(json.dumps(report.to_dict(), separators=(",", ":")))
        return 0 if report.clean else 1

    # Human-readable rendering.
    if report.window == 0:
        print("Chain integrity: EMPTY CHAIN (nothing to analyze)")
        return 0

    header = (
        f"Chain integrity: "
        f"{'CLEAN' if report.clean else f'{report.anomaly_count} ANOMALIES'} "
        f"(last {report.window} entries, seq "
        f"{report.first_seq}..{report.last_seq})"
    )
    print(header)
    print("-" * len(header))

    # Sequence
    if report.sequence_gaps:
        print(f"Sequence: {len(report.sequence_gaps)} gap(s)")
        for g in report.sequence_gaps:
            print(
                f"  WARN  seq {g.after_seq} -> {g.before_seq} "
                f"({g.missing_count} missing)"
            )
    else:
        print("Sequence: no gaps")

    # Timestamps
    if report.timestamp_anomalies:
        backward = [a for a in report.timestamp_anomalies if a.type == "backward"]
        large = [a for a in report.timestamp_anomalies if a.type == "large_gap"]
        bursts = [a for a in report.timestamp_anomalies if a.type == "burst"]
        bits = []
        if backward:
            bits.append(f"{len(backward)} backward")
        if large:
            bits.append(f"{len(large)} large-gap")
        if bursts:
            bits.append(f"{len(bursts)} burst")
        print(f"Timestamps: {', '.join(bits)}")
        for a in report.timestamp_anomalies[:5]:
            print(f"  WARN  seq={a.seq} type={a.type}  {a.detail}")
        if len(report.timestamp_anomalies) > 5:
            print(f"  ... +{len(report.timestamp_anomalies) - 5} more")
    else:
        print("Timestamps: no anomalies")

    # Hashes
    if report.hash_anomalies:
        print(f"Hashes: {len(report.hash_anomalies)} duplicate(s)")
        for h in report.hash_anomalies[:5]:
            print(f"  WARN  seq={h.seq}  {h.detail}")
    else:
        print("Hashes: no duplicates")

    # Provider shift
    if report.provider_shift:
        print("Provider distribution: SHIFT detected")
        for p, sh in sorted(
            report.provider_shift.shifts.items(),
            key=lambda x: -abs(x[1]["delta_pct"]),
        ):
            print(
                f"  WARN  {p}: {sh['baseline_pct']}% -> {sh['current_pct']}% "
                f"({sh['delta_pct']:+.1f}pp)"
            )
    else:
        print("Provider distribution: stable")

    # Rate
    if report.rate_anomalies:
        for r in report.rate_anomalies:
            print(
                f"Rate: {r.baseline_rate}/h -> {r.current_rate}/h "
                f"({r.change_pct:+.1f}%)"
            )
    else:
        print("Rate: stable")

    return 0 if report.clean else 1
