"""
capture_baseline.py — Capture GENA state snapshot for BIJOTEL post-deploy baseline.

Run on GENA host. Outputs structured JSON to stdout.

Usage:
    ssh root@gena 'python3 /tmp/capture_baseline.py' > baseline.json

Captures:
  1. Per-container docker stats (memory, CPU, network, block I/O)
  2. Disk usage (chain.db, traces.db, /data, /opt/substrate-v2)
  3. Chain + CAS counts (from /data/bijotel_chain.db)
  4. Traces count (from /data/traces.db)
  5. Cost burn rate pre-deploy (24h before 09:14 UTC)
  6. Tick rates per ecosystem (calls/min last 30 min, all 9 ecosystems)
  7. BIJOTEL chain integrity verify
  8. Container compose status

Marks containers cu BIJOTEL active vs control group (no BIJOTEL).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Container name -> has_bijotel mapping
BIJOTEL_CONTAINERS = {
    "gena-v3-atelier-1",
    "gena-v4-piata-1",
    "gena-v9-oracle-1",
    "gena-v8-ambasador-1",
}

DEPLOY_COMPLETED_AT = "2026-05-10T09:21:01Z"  # last V8 synthetic in chain
DEPLOY_STARTED_AT = "2026-05-10T09:14:00Z"  # first V3 synthetic chain entry

# Mount paths on GENA host (volume gena_shared-data → /var/lib/docker/volumes/...)
SHARED_DATA = "/var/lib/docker/volumes/gena_shared-data/_data"
CHAIN_DB = f"{SHARED_DATA}/bijotel_chain.db"
TRACES_DB = f"{SHARED_DATA}/traces.db"


def parse_size_to_bytes(s: str) -> int:
    """Convert '123.4MiB' / '1.5GB' / '512KiB' / '0B' to bytes."""
    s = s.strip()
    m = re.match(r"^([\d.]+)\s*([KMGT]?i?B)?$", s)
    if not m:
        return 0
    val = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    multipliers = {
        "B": 1,
        "KB": 1000, "KIB": 1024,
        "MB": 1_000_000, "MIB": 1024 * 1024,
        "GB": 1_000_000_000, "GIB": 1024 ** 3,
        "TB": 10 ** 12, "TIB": 1024 ** 4,
    }
    return int(val * multipliers.get(unit, 1))


def parse_io_pair(s: str) -> tuple[int, int]:
    """Parse '1.2MB / 3.4MB' to (rx_bytes, tx_bytes)."""
    parts = s.split("/")
    if len(parts) != 2:
        return (0, 0)
    return (parse_size_to_bytes(parts[0]), parse_size_to_bytes(parts[1]))


def parse_pct(s: str) -> float:
    """Parse '5.23%' to 5.23."""
    return float(s.strip().rstrip("%"))


def docker_stats() -> list[dict]:
    """Return per-container stats. Note: docker stats uses {{.Name}}, NOT {{.Names}}."""
    fmt = "{{.Name}}|{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}|{{.NetIO}}|{{.BlockIO}}"
    out = subprocess.check_output(
        ["docker", "stats", "--no-stream", "--format", fmt], text=True
    )
    rows = []
    for line in out.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 6:
            continue
        name, mem, mem_pct, cpu, net, block = parts
        mem_used_str, _, mem_limit_str = mem.partition(" / ")
        net_rx, net_tx = parse_io_pair(net)
        block_read, block_write = parse_io_pair(block)
        rows.append({
            "name": name,
            "has_bijotel": name in BIJOTEL_CONTAINERS,
            "mem_used_str": mem_used_str.strip(),
            "mem_used_bytes": parse_size_to_bytes(mem_used_str.strip()),
            "mem_pct": parse_pct(mem_pct),
            "cpu_pct": parse_pct(cpu),
            "net_rx_bytes": net_rx,
            "net_tx_bytes": net_tx,
            "block_read_bytes": block_read,
            "block_write_bytes": block_write,
        })
    return rows


def file_sizes() -> dict:
    """Sizes for chain.db, traces.db, /data, etc."""
    out = {}
    for label, path in [
        ("chain_db_bytes", CHAIN_DB),
        ("traces_db_bytes", TRACES_DB),
    ]:
        try:
            out[label] = os.path.getsize(path) if os.path.exists(path) else 0
        except OSError:
            out[label] = 0

    # /data total
    try:
        result = subprocess.run(
            ["du", "-sb", SHARED_DATA],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            out["data_total_bytes"] = int(result.stdout.split()[0])
    except (subprocess.TimeoutExpired, ValueError):
        out["data_total_bytes"] = -1

    return out


def chain_stats() -> dict:
    """Counts from bijotel_chain.db."""
    out = {"row_count": 0, "cas_count": 0, "verify_status": "UNKNOWN"}
    if not os.path.exists(CHAIN_DB):
        out["verify_status"] = "NO_DB"
        return out

    conn = sqlite3.connect(CHAIN_DB)
    try:
        out["row_count"] = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
        out["cas_count"] = conn.execute("SELECT COUNT(*) FROM cas").fetchone()[0]

        # Approx bytes per span
        if out["row_count"]:
            out["bytes_per_span_avg"] = round(
                os.path.getsize(CHAIN_DB) / out["row_count"]
            )
    finally:
        conn.close()

    # Verify integrity (subprocess to bijotel CLI in v3-atelier container)
    secret = os.environ.get("BIJOTEL_HMAC_SECRET", "")
    if secret:
        try:
            verify = subprocess.run(
                ["docker", "exec", "-e", f"BIJOTEL_HMAC_SECRET={secret}",
                 "gena-v3-atelier-1",
                 "bijotel", "verify", "--db", "/data/bijotel_chain.db"],
                capture_output=True, text=True, timeout=15,
            )
            if "Chain VALID" in verify.stdout:
                out["verify_status"] = "VALID"
            elif "BROKEN" in verify.stdout:
                out["verify_status"] = "BROKEN"
            else:
                out["verify_status"] = f"UNKNOWN: {verify.stdout[:100]}"
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            out["verify_status"] = f"VERIFY_ERROR: {e}"

    return out


def traces_stats() -> dict:
    """Counts from traces.db (substrate_v2_trace)."""
    out = {"row_count": 0}
    if not os.path.exists(TRACES_DB):
        return out

    conn = sqlite3.connect(TRACES_DB)
    try:
        out["row_count"] = conn.execute("SELECT COUNT(*) FROM trace_spans").fetchone()[0]
        # Per-ecosystem totals
        out["per_ecosystem_total"] = {
            row[0]: row[1] for row in conn.execute(
                "SELECT ecosystem, COUNT(*) FROM trace_spans GROUP BY ecosystem"
            )
        }
    finally:
        conn.close()

    return out


def cost_burn_pre_deploy_24h() -> list[dict]:
    """Cost burn rate 24h before deploy started (09:14 UTC)."""
    if not os.path.exists(TRACES_DB):
        return []

    conn = sqlite3.connect(TRACES_DB)
    try:
        rows = conn.execute("""
            SELECT
                ecosystem,
                COUNT(*) as calls,
                ROUND(SUM(cost_usd), 6) as cost_total,
                ROUND(AVG(cost_usd), 8) as cost_avg_per_call,
                MIN(created_at) as window_start,
                MAX(created_at) as window_end
            FROM trace_spans
            WHERE created_at < '2026-05-10 09:14:00'
              AND created_at > '2026-05-09 09:14:00'
            GROUP BY ecosystem
            ORDER BY cost_total DESC
        """).fetchall()
        return [
            {
                "ecosystem": r[0],
                "calls": r[1],
                "cost_total_usd": r[2],
                "cost_avg_per_call_usd": r[3],
                "window_start": r[4],
                "window_end": r[5],
            }
            for r in rows
        ]
    finally:
        conn.close()


def tick_rates_30min() -> list[dict]:
    """Calls per minute over last 30 min, per ecosystem (all 9)."""
    if not os.path.exists(TRACES_DB):
        return []

    conn = sqlite3.connect(TRACES_DB)
    try:
        rows = conn.execute("""
            SELECT
                ecosystem,
                COUNT(*) as calls_30min,
                ROUND(COUNT(*) * 1.0 / 30, 3) as calls_per_min
            FROM trace_spans
            WHERE created_at > datetime('now', '-30 minutes')
            GROUP BY ecosystem
            ORDER BY calls_30min DESC
        """).fetchall()
        return [
            {
                "ecosystem": r[0],
                "calls_30min": r[1],
                "calls_per_min": r[2],
            }
            for r in rows
        ]
    finally:
        conn.close()


def container_compose_status() -> list[dict]:
    """List all GENA containers with status."""
    fmt = "{{.Names}}|{{.State}}|{{.Status}}"
    out = subprocess.check_output(
        ["docker", "ps", "-a", "--filter", "name=gena-",
         "--format", fmt], text=True
    )
    rows = []
    for line in out.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        rows.append({
            "name": parts[0],
            "state": parts[1],
            "status": parts[2],
        })
    return rows


def main():
    baseline = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "deploy_started_at": DEPLOY_STARTED_AT,
        "deploy_completed_at": DEPLOY_COMPLETED_AT,
        "containers": docker_stats(),
        "files": file_sizes(),
        "chain": chain_stats(),
        "traces": traces_stats(),
        "cost_burn_pre_deploy_24h": cost_burn_pre_deploy_24h(),
        "tick_rates_30min_by_ecosystem": tick_rates_30min(),
        "compose_status": container_compose_status(),
    }
    print(json.dumps(baseline, indent=2))


if __name__ == "__main__":
    main()
