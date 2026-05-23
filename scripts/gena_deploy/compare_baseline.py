"""
compare_baseline.py - Compare current GENA state vs saved post-deploy baseline.

Usage:
    python compare_baseline.py <baseline.json>

Captures current state via remote SSH (uses same SSH keys as user, not embedded).
Diffs against baseline. Outputs tabular comparison + alerts.

Exit codes:
    0  ALL HEALTHY
    1  WARNINGS (no critical, but worth review)
    2  CRITICAL (chain broken / memory exploded / tick zero)

Thresholds:
    memory_growth_pct_max     +30%   per BIJOTEL container vs baseline
    tick_rate_drop_pct_max    -20%   per ecosystem vs baseline (organic noise tolerated)
    chain_integrity_required  True   bijotel verify must return VALID

Designed for daily checkpoint (~30s execute) — not deep forensics.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

THRESHOLDS = {
    "memory_growth_pct_max": 30.0,
    "tick_rate_drop_pct_max": 20.0,
    "chain_integrity_required": True,
}

# Path on GENA where capture_baseline.py lives (after first deploy)
GENA_HOST = "root@178.104.252.86"
GENA_CAPTURE_SCRIPT = "/tmp/capture_baseline.py"
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
SECRET_FILE = os.path.expanduser(
    "~/Desktop/AGENTY 2026/BIJUTERII S3/BIJUTERII IMPLEMENT/BIJOTEL/.env-gena"
)


def load_secret() -> str:
    """Read BIJOTEL_HMAC_SECRET from local backup file."""
    p = Path(SECRET_FILE)
    if not p.exists():
        return ""
    for line in p.read_text().splitlines():
        if line.startswith("BIJOTEL_HMAC_SECRET="):
            return line.split("=", 1)[1].strip()
    return ""


def capture_current() -> dict:
    """Run capture_baseline.py on GENA, return parsed dict."""
    secret = load_secret()
    cmd = [
        "ssh", "-i", SSH_KEY, GENA_HOST,
        f"BIJOTEL_HMAC_SECRET={secret} python3 {GENA_CAPTURE_SCRIPT}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"ERROR: capture_baseline.py failed: {result.stderr}", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON from capture: {e}", file=sys.stderr)
        print(f"Raw: {result.stdout[:500]}", file=sys.stderr)
        sys.exit(2)


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def fmt_pct_delta(pct: float) -> str:
    if pct > 0:
        return f"+{pct:.1f}%"
    return f"{pct:.1f}%"


def diff_containers(baseline: dict, current: dict, issues: list) -> None:
    """Compare per-container memory + CPU + I/O."""
    print()
    print("=== CONTAINER COMPARISON ===")
    print(f"{'Container':<26} {'Type':<10} {'Mem base':>10} {'Mem now':>10} {'Diff':>9} {'CPU base':>9} {'CPU now':>9}")
    print("-" * 90)

    base_by_name = {c["name"]: c for c in baseline["containers"]}
    cur_by_name = {c["name"]: c for c in current["containers"]}

    for name in sorted(set(base_by_name) | set(cur_by_name)):
        b = base_by_name.get(name)
        c = cur_by_name.get(name)
        tag = "[BIJOTEL]" if (c and c["has_bijotel"]) or (b and b["has_bijotel"]) else "[control]"
        if b is None:
            print(f"  {name:<24} {tag:<10} {'-':>10} {fmt_bytes(c['mem_used_bytes']):>10} {'NEW':>9}")
            continue
        if c is None:
            print(f"  {name:<24} {tag:<10} {fmt_bytes(b['mem_used_bytes']):>10} {'-':>10} {'GONE':>9}")
            issues.append(("WARN", f"Container {name} missing in current state"))
            continue

        b_mem = b["mem_used_bytes"]
        c_mem = c["mem_used_bytes"]
        if b_mem > 0:
            mem_delta_pct = ((c_mem - b_mem) / b_mem) * 100
        else:
            mem_delta_pct = 0.0

        print(
            f"  {name:<24} {tag:<10} "
            f"{fmt_bytes(b_mem):>10} {fmt_bytes(c_mem):>10} {fmt_pct_delta(mem_delta_pct):>9} "
            f"{b['cpu_pct']:>8.2f}% {c['cpu_pct']:>8.2f}%"
        )

        # Alert on memory growth — only for BIJOTEL containers
        if c.get("has_bijotel") and mem_delta_pct > THRESHOLDS["memory_growth_pct_max"]:
            issues.append((
                "WARN",
                f"{name} memory grew {mem_delta_pct:.1f}% (threshold {THRESHOLDS['memory_growth_pct_max']}%)"
            ))


def diff_chain(baseline: dict, current: dict, issues: list) -> None:
    """Chain growth + integrity."""
    print()
    print("=== CHAIN COMPARISON ===")
    b = baseline["chain"]
    c = current["chain"]

    print(f"  row_count:           {b['row_count']} -> {c['row_count']} (Diff +{c['row_count'] - b['row_count']})")
    print(f"  cas_count:           {b['cas_count']} -> {c['cas_count']} (Diff +{c['cas_count'] - b['cas_count']})")
    print(f"  verify_status:       {b['verify_status']} -> {c['verify_status']}")
    print(f"  bytes_per_span_avg:  {b.get('bytes_per_span_avg', '?')} -> {c.get('bytes_per_span_avg', '?')}")

    if THRESHOLDS["chain_integrity_required"] and c["verify_status"] != "VALID":
        issues.append(("CRITICAL", f"Chain integrity broken: {c['verify_status']}"))

    if c["row_count"] < b["row_count"]:
        issues.append(("CRITICAL", f"Chain rows decreased: {b['row_count']} -> {c['row_count']}"))


def diff_traces(baseline: dict, current: dict, issues: list) -> None:
    """Traces growth (substrate_v2_trace coexistence)."""
    print()
    print("=== TRACES COMPARISON (substrate_v2_trace) ===")
    b = baseline["traces"]
    c = current["traces"]

    print(f"  row_count:  {b['row_count']} -> {c['row_count']} (Diff +{c['row_count'] - b['row_count']})")

    if c["row_count"] < b["row_count"]:
        issues.append(("CRITICAL", "traces.db rows decreased — substrate_v2_trace possibly broken"))


def diff_tick_rates(baseline: dict, current: dict, issues: list) -> None:
    """Per-ecosystem tick rate comparison (last 30 min window each side)."""
    print()
    print("=== TICK RATES (last 30 min, each capture) ===")
    print(f"{'Ecosystem':<14} {'Base /min':>10} {'Cur /min':>10} {'Diff %':>10}")
    print("-" * 50)

    b_by_eco = {r["ecosystem"]: r for r in baseline["tick_rates_30min_by_ecosystem"]}
    c_by_eco = {r["ecosystem"]: r for r in current["tick_rates_30min_by_ecosystem"]}

    for eco in sorted(set(b_by_eco) | set(c_by_eco)):
        b = b_by_eco.get(eco, {"calls_per_min": 0})
        c = c_by_eco.get(eco, {"calls_per_min": 0})
        b_rate = b["calls_per_min"]
        c_rate = c["calls_per_min"]
        if b_rate > 0:
            delta_pct = ((c_rate - b_rate) / b_rate) * 100
        elif c_rate > 0:
            delta_pct = 100.0  # new traffic
        else:
            delta_pct = 0.0
        print(f"  {eco:<12} {b_rate:>10.3f} {c_rate:>10.3f} {fmt_pct_delta(delta_pct):>10}")

        # Alert on rate drop > threshold (only for ecosystems that had traffic)
        if b_rate > 0.05 and delta_pct < -THRESHOLDS["tick_rate_drop_pct_max"]:
            issues.append((
                "WARN",
                f"{eco} tick rate dropped {delta_pct:.1f}% (threshold -{THRESHOLDS['tick_rate_drop_pct_max']}%)"
            ))


def diff_files(baseline: dict, current: dict, issues: list) -> None:
    """File size changes (chain.db, traces.db, /data total)."""
    print()
    print("=== FILE SIZES ===")
    b = baseline["files"]
    c = current["files"]
    for k in sorted(set(b) | set(c)):
        b_v = b.get(k, 0)
        c_v = c.get(k, 0)
        delta = c_v - b_v
        delta_pct = (delta / b_v * 100) if b_v > 0 else 0.0
        print(f"  {k:<22} {fmt_bytes(b_v):>10} -> {fmt_bytes(c_v):>10} (+{fmt_bytes(delta) if delta >= 0 else fmt_bytes(-delta)}, {fmt_pct_delta(delta_pct)})")


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: compare_baseline.py <baseline.json>", file=sys.stderr)
        return 1

    baseline_path = Path(sys.argv[1])
    if not baseline_path.exists():
        print(f"ERROR: baseline not found: {baseline_path}", file=sys.stderr)
        return 1

    print(f"=== Loading baseline: {baseline_path.name} ===")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    print(f"Baseline captured at: {baseline['captured_at']}")

    print(f"\n=== Capturing current state from GENA ({GENA_HOST}) ===")
    current = capture_current()
    print(f"Current captured at:  {current['captured_at']}")

    # Time delta
    try:
        b_ts = datetime.fromisoformat(baseline["captured_at"].replace("Z", "+00:00"))
        c_ts = datetime.fromisoformat(current["captured_at"].replace("Z", "+00:00"))
        delta = c_ts - b_ts
        print(f"Time elapsed:         {delta}")
    except (ValueError, KeyError):
        pass

    issues: list = []

    diff_containers(baseline, current, issues)
    diff_files(baseline, current, issues)
    diff_chain(baseline, current, issues)
    diff_traces(baseline, current, issues)
    diff_tick_rates(baseline, current, issues)

    # Summary
    print()
    print("=" * 60)
    if not issues:
        print("ALL HEALTHY -- no issues detected")
        return 0

    critical = [i for i in issues if i[0] == "CRITICAL"]
    warnings = [i for i in issues if i[0] == "WARN"]

    if critical:
        print(f"CRITICAL ISSUES ({len(critical)}):")
        for level, msg in critical:
            print(f"  [{level}] {msg}")
    if warnings:
        print(f"WARNINGS ({len(warnings)}):")
        for level, msg in warnings:
            print(f"  [{level}] {msg}")

    return 2 if critical else 1


if __name__ == "__main__":
    sys.exit(main())
