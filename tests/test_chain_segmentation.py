"""End-to-end tests for chain segmentation features (v2.2.0).

Covers:
- Range verify: ``verify_chain(..., seq_start/seq_end/since_ns/until_ns/last_n)``
- Range export: ``export_chain(..., seq_start/...)`` produces ``segment`` block
- Verify-export accepts segment files with non-genesis boundary prev_hash
- Archive: ``archive_chain`` peels old rows into a new DB and deletes from source
- Continuity: ``verify_continuity`` walks N segments and reports per-pair status
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.processors import (
    HmacChainSpanProcessor,
    archive_chain,
    chain_range_summary,
    export_chain,
    verify_chain,
    verify_continuity,
    verify_export,
)

SECRET = b"x" * 32


@pytest.fixture
def chain_db(tmp_path: Path) -> Path:
    """Build a chain.db with 20 spans by emitting via real TracerProvider."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")
    for i in range(20):
        with tracer.start_as_current_span(f"span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 10 + i)
            span.set_attribute("gen_ai.usage.output_tokens", 5)
    provider.shutdown()
    return db


# ──────────────────────── verify_chain range modes ────────────────────────


def test_verify_chain_full_chain_still_works(chain_db: Path) -> None:
    """Backward compat: zero kwargs = full-chain verify (v1.x behaviour).

    Since v2.13.1 the success tuple reports the last verified seq (20 for
    this 20-span fixture); the valid flag is unchanged + authoritative.
    """
    valid, seq, reason = verify_chain(chain_db, SECRET)
    assert valid is True
    assert seq == 20  # last verified seq (fixture = 20 spans), not None
    assert reason is None


def test_verify_chain_range_middle_segment(chain_db: Path) -> None:
    """seq 5..15 verifies cleanly when its predecessor is still in the DB."""
    valid, seq, reason = verify_chain(chain_db, SECRET, seq_start=5, seq_end=15)
    assert valid is True, reason
    assert seq == 15  # last seq in the verified range


def test_verify_chain_last_n(chain_db: Path) -> None:
    """--last 5 verifies the tail of the chain."""
    valid, seq, reason = verify_chain(chain_db, SECRET, last_n=5)
    assert valid is True, reason
    assert seq == 20  # tail of a 20-span chain ends at seq 20


def test_verify_chain_last_n_greater_than_chain(chain_db: Path) -> None:
    """If last_n > chain length, verify whatever exists (no crash)."""
    valid, seq, reason = verify_chain(chain_db, SECRET, last_n=9999)
    assert valid is True, reason


def test_verify_chain_since_ns_filters_by_timestamp(chain_db: Path) -> None:
    """Filter on timestamp_ns — for our fast synthetic chain everything
    is in the same instant, so since_ns=0 selects everything."""
    valid, seq, reason = verify_chain(chain_db, SECRET, since_ns=0)
    assert valid is True, reason


def test_verify_chain_range_detects_tamper_inside_window(chain_db: Path) -> None:
    """Tampering an entry inside the verified range is caught."""
    with sqlite3.connect(chain_db) as conn:
        conn.execute(
            "UPDATE chain SET canonical_hash = 'f' * 64 WHERE seq = 10"
        )
        conn.commit()
    valid, seq, reason = verify_chain(chain_db, SECRET, seq_start=5, seq_end=15)
    assert valid is False
    assert seq == 10
    assert reason and "canonical_hash" in reason


def test_verify_chain_range_outside_tamper_window_passes(chain_db: Path) -> None:
    """Tamper at seq=15 does NOT trip a verify of seq 5..10."""
    with sqlite3.connect(chain_db) as conn:
        conn.execute(
            "UPDATE chain SET canonical_hash = 'f' * 64 WHERE seq = 15"
        )
        conn.commit()
    valid, seq, reason = verify_chain(chain_db, SECRET, seq_start=5, seq_end=10)
    assert valid is True, reason


# ──────────────────────── chain_range_summary ────────────────────────


def test_chain_range_summary_full_chain(chain_db: Path) -> None:
    s = chain_range_summary(chain_db)
    assert s["first_seq"] == 1
    assert s["last_seq"] == 20
    assert s["count"] == 20
    assert s["boundary_predecessor_in_db"] is False


def test_chain_range_summary_range(chain_db: Path) -> None:
    s = chain_range_summary(chain_db, seq_start=5, seq_end=10)
    assert s["first_seq"] == 5
    assert s["last_seq"] == 10
    assert s["count"] == 6
    assert s["boundary_predecessor_in_db"] is True


def test_chain_range_summary_empty(chain_db: Path) -> None:
    s = chain_range_summary(chain_db, seq_start=999, seq_end=1000)
    assert s["count"] == 0
    assert s["first_seq"] is None


# ──────────────────────── export_chain range modes ────────────────────────


def test_export_chain_full_has_no_segment_block(chain_db: Path, tmp_path: Path) -> None:
    """Full-chain export keeps the historical v1/v2 shape — no segment."""
    out = tmp_path / "full.json"
    export_chain(chain_db, out, SECRET)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "segment" not in data


def test_export_chain_range_adds_segment_block(chain_db: Path, tmp_path: Path) -> None:
    """Range export carries first_seq, last_seq, total_in_full_chain, boundary."""
    out = tmp_path / "seg.json"
    export_chain(chain_db, out, SECRET, seq_start=5, seq_end=15)
    data = json.loads(out.read_text(encoding="utf-8"))
    seg = data["segment"]
    assert seg["first_seq"] == 5
    assert seg["last_seq"] == 15
    assert seg["total_in_segment"] == 11
    assert seg["total_in_full_chain"] == 20
    assert seg["is_complete_chain"] is False
    assert len(seg["boundary_prev_hash"]) == 64


def test_verify_export_segment_passes(chain_db: Path, tmp_path: Path) -> None:
    """A range-exported segment file verifies under verify_export."""
    out = tmp_path / "seg.json"
    export_chain(chain_db, out, SECRET, seq_start=5, seq_end=15)
    valid, reason = verify_export(out, SECRET)
    assert valid is True, reason


def test_verify_export_segment_detects_boundary_tamper(
    chain_db: Path, tmp_path: Path
) -> None:
    """Mutating segment.boundary_prev_hash breaks the first-row check."""
    out = tmp_path / "seg.json"
    export_chain(chain_db, out, SECRET, seq_start=5, seq_end=15)
    data = json.loads(out.read_text(encoding="utf-8"))
    data["segment"]["boundary_prev_hash"] = "f" * 64
    out.write_text(json.dumps(data), encoding="utf-8")
    valid, reason = verify_export(out, SECRET)
    assert valid is False
    assert reason and "prev_hash" in reason


def test_export_chain_last_n(chain_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "last.json"
    export_chain(chain_db, out, SECRET, last_n=5)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["segment"]["total_in_segment"] == 5
    assert data["segment"]["first_seq"] == 16
    assert data["segment"]["last_seq"] == 20


# ──────────────────────── archive_chain ────────────────────────


def test_archive_creates_valid_db_and_deletes_from_source(
    chain_db: Path, tmp_path: Path
) -> None:
    """The happy path: archive seq 1..10, source DB keeps seq 11..20."""
    archive_path = tmp_path / "archive_old.db"
    report = archive_chain(
        chain_db, archive_path, SECRET, before_seq=11
    )
    assert report["dry_run"] is False
    assert report["archived_count"] == 10
    assert report["first_seq"] == 1
    assert report["last_seq"] == 10
    assert report["main_remaining_count"] == 10
    # Archive must verify independently.
    valid, seq, reason = verify_chain(archive_path, SECRET)
    assert valid is True, reason
    # Source must still verify (genesis boundary now starts at the new
    # first row — verify_chain handles that path).
    valid2, seq2, reason2 = verify_chain(chain_db, SECRET)
    assert valid2 is True, reason2


def test_archive_dry_run_changes_nothing(chain_db: Path, tmp_path: Path) -> None:
    archive_path = tmp_path / "archive_dry.db"
    report = archive_chain(
        chain_db, archive_path, SECRET, before_seq=11, dry_run=True
    )
    assert report["dry_run"] is True
    assert report["archived_count"] == 10
    assert not archive_path.exists()
    # Source untouched.
    with sqlite3.connect(chain_db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
    assert total == 20


def test_archive_boundary_matches_source(chain_db: Path, tmp_path: Path) -> None:
    """Archive's last_hmac_hash == source's first prev_hash after archive."""
    archive_path = tmp_path / "archive_bound.db"
    report = archive_chain(
        chain_db, archive_path, SECRET, before_seq=11
    )
    archive_last_hmac = report["last_hmac_hash"]
    with sqlite3.connect(chain_db) as conn:
        first_row = conn.execute(
            "SELECT prev_hash FROM chain ORDER BY seq ASC LIMIT 1"
        ).fetchone()
    assert first_row[0] == archive_last_hmac


def test_archive_refuses_to_overwrite_existing_path(
    chain_db: Path, tmp_path: Path
) -> None:
    archive_path = tmp_path / "exists.db"
    archive_path.write_text("not actually a SQLite file", encoding="utf-8")
    with pytest.raises(FileExistsError):
        archive_chain(chain_db, archive_path, SECRET, before_seq=5)


def test_archive_with_sign_key_emits_signed_sidecar(
    chain_db: Path, tmp_path: Path
) -> None:
    """When sign_key_path is supplied, a signed JSON sidecar is produced."""
    from bijotel.crypto.ed25519 import generate_keypair
    priv, pub = generate_keypair()
    priv_path = tmp_path / "ed_priv.pem"
    pub_path = tmp_path / "ed_pub.pem"
    priv_path.write_bytes(priv)
    pub_path.write_bytes(pub)

    archive_path = tmp_path / "signed_archive.db"
    report = archive_chain(
        chain_db, archive_path, SECRET,
        before_seq=11, sign_key_path=priv_path,
    )
    sidecar = Path(report["segment_json_path"])
    assert sidecar.exists()
    # Auditor-mode verify with pubkey only.
    valid, reason = verify_export(sidecar, secret_key=None, public_key_path=pub_path)
    assert valid is True, reason


def test_archive_requires_exactly_one_before_filter(
    chain_db: Path, tmp_path: Path
) -> None:
    archive_path = tmp_path / "x.db"
    with pytest.raises(ValueError):
        archive_chain(chain_db, archive_path, SECRET)
    with pytest.raises(ValueError):
        archive_chain(
            chain_db, archive_path, SECRET, before_seq=5, before_ns=1
        )


# ──────────────────────── verify_continuity ────────────────────────


def test_continuity_two_segments_match(chain_db: Path, tmp_path: Path) -> None:
    archive_path = tmp_path / "two_seg_a.db"
    archive_chain(chain_db, archive_path, SECRET, before_seq=11)
    result = verify_continuity([archive_path, chain_db])
    assert result["valid"] is True
    assert len(result["segments"]) == 2
    assert len(result["boundaries"]) == 1
    assert result["boundaries"][0]["matches"] is True


def test_continuity_three_segments(chain_db: Path, tmp_path: Path) -> None:
    """Two archives + live chain — must form a continuous run."""
    a1 = tmp_path / "seg1.db"
    a2 = tmp_path / "seg2.db"
    # Archive seq 1..5 first.
    archive_chain(chain_db, a1, SECRET, before_seq=6)
    # Then archive seq 6..12 from the now-trimmed source.
    archive_chain(chain_db, a2, SECRET, before_seq=13)
    # Source DB now holds seq 13..20.
    result = verify_continuity([a1, a2, chain_db])
    assert result["valid"] is True
    assert len(result["segments"]) == 3
    assert all(b["matches"] for b in result["boundaries"])
    total = sum(s["count"] for s in result["segments"])
    assert total == 20


def test_continuity_detects_gap(chain_db: Path, tmp_path: Path) -> None:
    """If the boundary hashes don't line up, continuity reports a BREAK."""
    archive_path = tmp_path / "gap_arch.db"
    archive_chain(chain_db, archive_path, SECRET, before_seq=11)
    # Tamper with the archive's archive_meta.last_hmac_hash to simulate a gap.
    with sqlite3.connect(archive_path) as conn:
        conn.execute(
            "UPDATE archive_meta SET value = ? WHERE key = 'last_hmac_hash'",
            ("f" * 64,),
        )
        conn.commit()
    result = verify_continuity([archive_path, chain_db])
    assert result["valid"] is False
    assert result["boundaries"][0]["matches"] is False


def test_continuity_missing_file(tmp_path: Path) -> None:
    result = verify_continuity([tmp_path / "nope.db"])
    assert result["valid"] is False
    assert result["segments"][0]["reason"] == "file not found"
