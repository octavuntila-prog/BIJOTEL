"""CLI subprocess tests for ``bijotel archive`` and ``bijotel verify-continuity`` (v2.2.0).

Coverage gap caught by internal audit 2026-05-26: the v2.2.0 segmentation
features have library-level tests (``test_chain_segmentation.py``) but
the CLI argparse + handler paths were uncovered.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.processors import HmacChainSpanProcessor

SECRET_HEX = "78" * 32  # 32 bytes -- equivalent to b"x" * 32


@pytest.fixture
def chain_db_with_entries(tmp_path: Path) -> Path:
    """Build a chain.db with 10 sealed spans for CLI testing."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db, secret_key=bytes.fromhex(SECRET_HEX))
    )
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("cli-archive-test")
    for i in range(10):
        with tracer.start_as_current_span(f"span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 10)
            span.set_attribute("gen_ai.usage.output_tokens", 5)
    provider.shutdown()
    return db


def _run_cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `python -m bijotel.cli.main <args>`.

    Force UTF-8 stdout encoding so unicode arrows / box chars in CLI
    output don't crash subprocess on Windows (default cp1252 chokes on
    `→` which the segmentation CLI uses for "first→last" ranges).
    """
    import os
    full_env = os.environ.copy()
    full_env["BIJOTEL_HMAC_SECRET"] = SECRET_HEX
    full_env["PYTHONIOENCODING"] = "utf-8"
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "bijotel.cli.main", *args],
        capture_output=True, text=True, check=False, env=full_env,
        encoding="utf-8",
    )


# ──────────────────────── bijotel archive ────────────────────────


def test_archive_dry_run_no_filesystem_changes(
    chain_db_with_entries: Path, tmp_path: Path
) -> None:
    """--dry-run reports the plan without writing the archive DB."""
    archive_path = tmp_path / "archive_dry.db"
    res = _run_cli(
        "archive", "--db", str(chain_db_with_entries),
        "--output", str(archive_path),
        "--before-seq", "6",
        "--dry-run",
    )
    assert res.returncode == 0, res.stderr
    assert "DRY RUN" in res.stdout
    assert not archive_path.exists()


def test_archive_creates_valid_db(
    chain_db_with_entries: Path, tmp_path: Path
) -> None:
    """Real archive: peel seq 1..5 off, leaving seq 6..10 in source."""
    archive_path = tmp_path / "archive_real.db"
    res = _run_cli(
        "archive", "--db", str(chain_db_with_entries),
        "--output", str(archive_path),
        "--before-seq", "6",
    )
    assert res.returncode == 0, res.stderr
    assert archive_path.exists()
    assert "Archived 5 entries" in res.stdout
    assert "seq 1 → 5" in res.stdout
    # Source now has only seq 6..10
    res2 = _run_cli("verify", "--db", str(chain_db_with_entries))
    assert res2.returncode == 0, res2.stderr
    assert "VALID" in res2.stdout
    # Archive itself verifies.
    res3 = _run_cli("verify", "--db", str(archive_path))
    assert res3.returncode == 0, res3.stderr
    assert "VALID" in res3.stdout


def test_archive_refuses_to_overwrite_existing(
    chain_db_with_entries: Path, tmp_path: Path
) -> None:
    archive_path = tmp_path / "existing.db"
    archive_path.write_text("not a db")
    res = _run_cli(
        "archive", "--db", str(chain_db_with_entries),
        "--output", str(archive_path),
        "--before-seq", "5",
    )
    assert res.returncode != 0
    assert "already" in res.stderr.lower() or "exist" in res.stderr.lower()


def test_archive_requires_one_before_filter(
    chain_db_with_entries: Path, tmp_path: Path
) -> None:
    """Pass neither --before nor --before-seq → exit 2 with clear message."""
    res = _run_cli(
        "archive", "--db", str(chain_db_with_entries),
        "--output", str(tmp_path / "x.db"),
    )
    assert res.returncode == 2
    assert "exactly one" in res.stderr.lower()


def test_archive_with_sign_key_writes_sidecar(
    chain_db_with_entries: Path, tmp_path: Path
) -> None:
    """--sign-key emits a JSON sidecar verifiable with public key alone."""
    keys_dir = tmp_path / "k"
    _run_cli("keygen", "--output-dir", str(keys_dir))
    priv = keys_dir / "bijotel_private.pem"
    pub = keys_dir / "bijotel_public.pem"

    archive_path = tmp_path / "signed_arch.db"
    res = _run_cli(
        "archive", "--db", str(chain_db_with_entries),
        "--output", str(archive_path),
        "--before-seq", "6",
        "--sign-key", str(priv),
    )
    assert res.returncode == 0, res.stderr
    sidecar = archive_path.with_suffix(".json")
    assert sidecar.exists(), "sidecar JSON not written"
    # Auditor mode verify: public key alone, no HMAC secret in env.
    res2 = _run_cli(
        "verify-export", str(sidecar),
        "--public-key", str(pub),
        env={"BIJOTEL_HMAC_SECRET": ""},
    )
    assert res2.returncode == 0, res2.stderr
    assert "VALID" in res2.stdout


# ──────────────────────── bijotel verify-continuity ────────────────────────


def test_verify_continuity_two_segments(
    chain_db_with_entries: Path, tmp_path: Path
) -> None:
    """Archive seq 1..5, then verify continuity across (archive, live)."""
    archive_path = tmp_path / "seg1.db"
    _run_cli(
        "archive", "--db", str(chain_db_with_entries),
        "--output", str(archive_path),
        "--before-seq", "6",
    )
    res = _run_cli(
        "verify-continuity",
        str(archive_path), str(chain_db_with_entries),
    )
    assert res.returncode == 0, res.stderr
    assert "CONTINUOUS" in res.stdout
    # Per-pair status line
    assert "OK" in res.stdout


def test_verify_continuity_detects_gap(
    chain_db_with_entries: Path, tmp_path: Path
) -> None:
    """Tamper the archive's archive_meta last_hmac to simulate a gap."""
    archive_path = tmp_path / "seg_gap.db"
    _run_cli(
        "archive", "--db", str(chain_db_with_entries),
        "--output", str(archive_path),
        "--before-seq", "6",
    )
    import sqlite3
    with sqlite3.connect(archive_path) as conn:
        conn.execute(
            "UPDATE archive_meta SET value = ? WHERE key = 'last_hmac_hash'",
            ("f" * 64,),
        )
        conn.commit()
    res = _run_cli(
        "verify-continuity",
        str(archive_path), str(chain_db_with_entries),
    )
    assert res.returncode != 0
    assert "BREAK" in res.stdout or "FAIL" in res.stderr


# ──────────────────────── bijotel verify with range ────────────────────────


def test_verify_range_inside_chain(chain_db_with_entries: Path) -> None:
    """`bijotel verify --range 3:7` reports a segment-mode VALID."""
    res = _run_cli(
        "verify", "--db", str(chain_db_with_entries),
        "--range", "3:7",
    )
    assert res.returncode == 0, res.stderr
    assert "VALID" in res.stdout
    assert "segment" in res.stdout.lower()


def test_verify_last_n(chain_db_with_entries: Path) -> None:
    """`bijotel verify --last 3` verifies the tail."""
    res = _run_cli(
        "verify", "--db", str(chain_db_with_entries),
        "--last", "3",
    )
    assert res.returncode == 0, res.stderr
    assert "VALID" in res.stdout
