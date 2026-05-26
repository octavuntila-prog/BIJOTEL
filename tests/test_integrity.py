"""Tests for chain-integrity monitor (v2.8.0).

Covers each detector independently (hand-seeded SQLite rows so we can
trigger specific patterns: gaps, backward timestamps, burst, dup
canonical_hash, provider shift, rate change), then the aggregate
``analyze()``, the ``IntegrityReport`` dataclass behaviour, the CLI
subprocess, and the REST endpoint.

The fixtures build chain.db files row-by-row rather than going through
``HmacChainSpanProcessor`` — we don't need real HMACs for these tests,
only the schema. This keeps the tests fast (no OpenTelemetry, no
cryptography) and lets us trigger anomalies that a real writer would
never produce (duplicate canonical_hash, for instance).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bijotel.api.app import create_app
from bijotel.integrity import (
    ChainIntegrityMonitor,
    IntegrityReport,
    analyze_chain_integrity,
)
from bijotel.integrity.monitor import (
    BURST_THRESHOLD,
    LARGE_GAP_SEC,
    PROVIDER_SHIFT_PCT,
)

# ---------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------


def _init_chain_db(path: Path) -> None:
    """Create the minimal schema the monitor reads from."""
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE chain (
                seq INTEGER PRIMARY KEY,
                timestamp_ns INTEGER,
                canonical_hash TEXT,
                prev_hash TEXT,
                canonical_body BLOB
            )
            """
        )


def _seed(
    path: Path,
    *,
    n: int = 50,
    start_seq: int = 1,
    start_ts_ns: int = 1_700_000_000_000_000_000,  # ~2023-11-14 UTC
    spacing_ns: int = 180_000_000_000,             # 3 min between entries → 20/h
    provider: str = "anthropic",
    seq_skip: dict[int, int] | None = None,       # {after_index: gap_size}
    overrides: dict[int, dict] | None = None,     # per-index overrides
) -> None:
    """Insert ``n`` synthetic rows starting at ``start_seq``.

    ``seq_skip``  — at index i, jump seq by (gap_size+1) instead of 1.
    ``overrides`` — at index i, replace any column in the row dict.
    """
    _init_chain_db(path)
    rows = []
    seq = start_seq
    ts = start_ts_ns
    for i in range(n):
        body = json.dumps(
            {"attributes": {"gen_ai.provider.name": provider}}
        ).encode("utf-8")
        row = {
            "seq": seq,
            "timestamp_ns": ts,
            "canonical_hash": f"hash-{seq:08d}",
            "prev_hash": f"hash-{seq - 1:08d}" if seq > 1 else "0" * 64,
            "canonical_body": body,
        }
        if overrides and i in overrides:
            row.update(overrides[i])
        rows.append(row)

        # Advance seq (with optional skip).
        skip = (seq_skip or {}).get(i, 0)
        seq += 1 + skip
        ts += spacing_ns

    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO chain (seq, timestamp_ns, canonical_hash, "
            "prev_hash, canonical_body) VALUES "
            "(:seq, :timestamp_ns, :canonical_hash, :prev_hash, :canonical_body)",
            rows,
        )


# ---------------------------------------------------------------------
# 1. Sequence gaps
# ---------------------------------------------------------------------


def test_no_gaps_clean(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=30)
    report = analyze_chain_integrity(db, window=30)
    assert report.sequence_gaps == []
    assert report.clean is True


def test_single_gap_detected(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=20, seq_skip={5: 2})  # skip 2 between idx 5 and 6
    report = analyze_chain_integrity(db, window=30)
    assert len(report.sequence_gaps) == 1
    g = report.sequence_gaps[0]
    assert g.missing_count == 2
    assert g.before_seq - g.after_seq == 3
    assert report.clean is False


def test_multiple_gaps_detected(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=20, seq_skip={3: 1, 10: 5})
    report = analyze_chain_integrity(db, window=30)
    assert len(report.sequence_gaps) == 2
    total_missing = sum(g.missing_count for g in report.sequence_gaps)
    assert total_missing == 6


# ---------------------------------------------------------------------
# 2. Timestamps
# ---------------------------------------------------------------------


def test_normal_timestamps_clean(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=30)
    report = analyze_chain_integrity(db, window=30)
    assert report.timestamp_anomalies == []


def test_backward_timestamp_detected(tmp_path: Path) -> None:
    """Row 10's timestamp is earlier than row 9's."""
    db = tmp_path / "chain.db"
    base_ts = 1_700_000_000_000_000_000
    _seed(
        db,
        n=20,
        overrides={
            10: {"timestamp_ns": base_ts + 5 * 180_000_000_000 - 10_000_000_000},
        },
    )
    report = analyze_chain_integrity(db, window=30)
    backward = [a for a in report.timestamp_anomalies if a.type == "backward"]
    assert len(backward) >= 1
    assert backward[0].delta_sec < 0


def test_large_gap_detected(tmp_path: Path) -> None:
    """One 2-hour pause shows up as a large_gap anomaly."""
    db = tmp_path / "chain.db"
    base_ts = 1_700_000_000_000_000_000
    # Push row 5's timestamp 2 hours past the previous row.
    huge = base_ts + 4 * 180_000_000_000 + int(LARGE_GAP_SEC * 2 * 1e9)
    _seed(db, n=20, overrides={5: {"timestamp_ns": huge}})
    report = analyze_chain_integrity(db, window=30)
    large = [a for a in report.timestamp_anomalies if a.type == "large_gap"]
    assert len(large) >= 1
    assert large[0].delta_sec > LARGE_GAP_SEC


def test_burst_detected(tmp_path: Path) -> None:
    """N rows landing in the same wall-clock second triggers a burst."""
    db = tmp_path / "chain.db"
    # Spacing of 50ms — 20 rows land in <2 seconds, > BURST_THRESHOLD per sec.
    _seed(db, n=BURST_THRESHOLD + 5, spacing_ns=50_000_000)
    report = analyze_chain_integrity(db, window=BURST_THRESHOLD + 5)
    bursts = [a for a in report.timestamp_anomalies if a.type == "burst"]
    assert len(bursts) >= 1
    assert "1 second" in bursts[0].detail


# ---------------------------------------------------------------------
# 3. Hashes
# ---------------------------------------------------------------------


def test_unique_hashes_clean(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=30)
    report = analyze_chain_integrity(db, window=30)
    assert report.hash_anomalies == []


def test_duplicate_canonical_hash_detected(tmp_path: Path) -> None:
    """Two rows sharing canonical_hash trigger a HashAnomaly on the second."""
    db = tmp_path / "chain.db"
    _seed(
        db,
        n=20,
        overrides={
            8: {"canonical_hash": "hash-00000003"},  # collide with seq=3
        },
    )
    report = analyze_chain_integrity(db, window=30)
    assert len(report.hash_anomalies) == 1
    h = report.hash_anomalies[0]
    assert h.type == "duplicate_canonical"
    assert "seq=3" in h.detail


# ---------------------------------------------------------------------
# 4. Provider distribution
# ---------------------------------------------------------------------


def test_stable_provider_clean(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=40, provider="anthropic")
    report = analyze_chain_integrity(db, window=40)
    assert report.provider_shift is None


def test_provider_shift_detected(tmp_path: Path) -> None:
    """First half all anthropic, second half all xai → giant shift."""
    db = tmp_path / "chain.db"
    _init_chain_db(db)
    base_ts = 1_700_000_000_000_000_000
    rows = []
    for i in range(40):
        provider = "anthropic" if i < 20 else "xai"
        body = json.dumps(
            {"attributes": {"gen_ai.provider.name": provider}}
        ).encode("utf-8")
        rows.append({
            "seq": i + 1,
            "timestamp_ns": base_ts + i * 180_000_000_000,
            "canonical_hash": f"hash-{i:08d}",
            "prev_hash": f"hash-{i - 1:08d}" if i > 0 else "0" * 64,
            "canonical_body": body,
        })
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO chain VALUES "
            "(:seq, :timestamp_ns, :canonical_hash, :prev_hash, :canonical_body)",
            rows,
        )
    report = analyze_chain_integrity(db, window=40)
    assert report.provider_shift is not None
    shifts = report.provider_shift.shifts
    assert "anthropic" in shifts
    assert "xai" in shifts
    # anthropic: 100% → 0% (delta = -100), xai: 0% → 100% (delta = +100)
    assert shifts["anthropic"]["delta_pct"] < -PROVIDER_SHIFT_PCT
    assert shifts["xai"]["delta_pct"] > PROVIDER_SHIFT_PCT


# ---------------------------------------------------------------------
# 5. Rate
# ---------------------------------------------------------------------


def test_stable_rate_clean(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=40, spacing_ns=180_000_000_000)
    report = analyze_chain_integrity(db, window=40)
    assert report.rate_anomalies == []


def test_rate_drop_detected(tmp_path: Path) -> None:
    """First half: 20 rows in 1h. Second half: 20 rows in 4h → -75% rate."""
    db = tmp_path / "chain.db"
    _init_chain_db(db)
    base_ts = 1_700_000_000_000_000_000
    rows = []
    # First half: 3 min spacing (20/h)
    ts = base_ts
    for i in range(20):
        rows.append({
            "seq": i + 1,
            "timestamp_ns": ts,
            "canonical_hash": f"h-{i:08d}",
            "prev_hash": f"h-{i - 1:08d}" if i > 0 else "0" * 64,
            "canonical_body": json.dumps({"attributes": {}}).encode(),
        })
        ts += 180_000_000_000
    # Second half: 12 min spacing (5/h)
    for i in range(20, 40):
        rows.append({
            "seq": i + 1,
            "timestamp_ns": ts,
            "canonical_hash": f"h-{i:08d}",
            "prev_hash": f"h-{i - 1:08d}",
            "canonical_body": json.dumps({"attributes": {}}).encode(),
        })
        ts += 720_000_000_000
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO chain VALUES "
            "(:seq, :timestamp_ns, :canonical_hash, :prev_hash, :canonical_body)",
            rows,
        )
    report = analyze_chain_integrity(db, window=40)
    assert len(report.rate_anomalies) == 1
    r = report.rate_anomalies[0]
    assert r.change_pct < -50  # significant drop
    assert r.current_rate < r.baseline_rate


def test_rate_spike_detected(tmp_path: Path) -> None:
    """Inverse: rate jumps up sharply in the second half."""
    db = tmp_path / "chain.db"
    _init_chain_db(db)
    base_ts = 1_700_000_000_000_000_000
    rows = []
    ts = base_ts
    # First half: 12 min spacing
    for i in range(20):
        rows.append({
            "seq": i + 1,
            "timestamp_ns": ts,
            "canonical_hash": f"h-{i:08d}",
            "prev_hash": f"h-{i - 1:08d}" if i > 0 else "0" * 64,
            "canonical_body": json.dumps({"attributes": {}}).encode(),
        })
        ts += 720_000_000_000
    # Second half: 3 min spacing
    for i in range(20, 40):
        rows.append({
            "seq": i + 1,
            "timestamp_ns": ts,
            "canonical_hash": f"h-{i:08d}",
            "prev_hash": f"h-{i - 1:08d}",
            "canonical_body": json.dumps({"attributes": {}}).encode(),
        })
        ts += 180_000_000_000
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO chain VALUES "
            "(:seq, :timestamp_ns, :canonical_hash, :prev_hash, :canonical_body)",
            rows,
        )
    report = analyze_chain_integrity(db, window=40)
    assert len(report.rate_anomalies) == 1
    assert report.rate_anomalies[0].change_pct > 50


# ---------------------------------------------------------------------
# 6. Aggregate report
# ---------------------------------------------------------------------


def test_full_analyze_clean(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=50)
    report = analyze_chain_integrity(db, window=50)
    assert isinstance(report, IntegrityReport)
    assert report.clean is True
    assert report.anomaly_count == 0
    assert report.window == 50
    assert report.first_seq == 1
    assert report.last_seq == 50


def test_full_analyze_with_anomalies(tmp_path: Path) -> None:
    """Combine sequence gap + duplicate hash; ensure counts add up."""
    db = tmp_path / "chain.db"
    _seed(
        db,
        n=30,
        seq_skip={5: 1},
        overrides={10: {"canonical_hash": "hash-00000003"}},
    )
    report = analyze_chain_integrity(db, window=40)
    assert report.clean is False
    assert len(report.sequence_gaps) == 1
    assert len(report.hash_anomalies) == 1
    assert report.anomaly_count >= 2


def test_to_dict_json_serializable(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=30, seq_skip={3: 1})
    report = analyze_chain_integrity(db, window=30)
    d = report.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["clean"] is False
    assert d["sequence_gaps"][0]["missing_count"] == 1


def test_empty_chain_returns_zero_window(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _init_chain_db(db)
    report = analyze_chain_integrity(db, window=100)
    assert report.window == 0
    assert report.first_seq is None
    assert report.last_seq is None
    assert report.clean is True


def test_monitor_raises_when_db_missing(tmp_path: Path) -> None:
    bad = tmp_path / "does-not-exist.db"
    monitor = ChainIntegrityMonitor(bad, window=10)
    with pytest.raises(FileNotFoundError):
        monitor.analyze()


# ---------------------------------------------------------------------
# 7. CLI
# ---------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "bijotel.cli.main", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_cli_integrity_clean_exit_0(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=30)
    out = _run_cli("integrity", "--db", str(db), "--window", "30")
    assert out.returncode == 0, out.stderr
    assert "CLEAN" in out.stdout
    assert "Sequence: no gaps" in out.stdout


def test_cli_integrity_with_anomalies_exit_1(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=20, seq_skip={5: 2})
    out = _run_cli("integrity", "--db", str(db), "--window", "30")
    assert out.returncode == 1
    assert "ANOMAL" in out.stdout


def test_cli_integrity_json_output(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=20, seq_skip={5: 1})
    out = _run_cli("integrity", "--db", str(db), "--window", "30", "--json")
    assert out.returncode == 1
    payload = json.loads(out.stdout.strip())
    assert payload["clean"] is False
    assert len(payload["sequence_gaps"]) == 1


def test_cli_integrity_db_missing_exit_2(tmp_path: Path) -> None:
    out = _run_cli("integrity", "--db", str(tmp_path / "nope.db"))
    assert out.returncode == 2
    assert "not found" in out.stderr


# ---------------------------------------------------------------------
# 8. REST endpoint
# ---------------------------------------------------------------------


def test_api_integrity_returns_200_clean(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seed(db, n=30)
    client = TestClient(create_app(db_path=str(db)))
    r = client.get("/integrity?window=30")
    assert r.status_code == 200
    body = r.json()
    assert body["clean"] is True
    assert body["anomaly_count"] == 0


def test_api_integrity_returns_200_with_anomalies(tmp_path: Path) -> None:
    """Anomalies are 200 + body says clean=False; 4xx is reserved for
    'analysis itself could not run'."""
    db = tmp_path / "chain.db"
    _seed(db, n=20, seq_skip={3: 1})
    client = TestClient(create_app(db_path=str(db)))
    r = client.get("/integrity?window=30")
    assert r.status_code == 200
    body = r.json()
    assert body["clean"] is False
    assert len(body["sequence_gaps"]) >= 1


def test_api_integrity_db_missing_503(tmp_path: Path) -> None:
    client = TestClient(create_app(db_path=str(tmp_path / "nope.db")))
    r = client.get("/integrity")
    assert r.status_code == 503


# ---------------------------------------------------------------------
# 9. Public API
# ---------------------------------------------------------------------


def test_public_api_exports() -> None:
    import bijotel

    for name in ("ChainIntegrityMonitor", "IntegrityReport", "analyze_chain_integrity"):
        assert hasattr(bijotel, name), f"bijotel.{name} missing"
        assert name in bijotel.__all__
