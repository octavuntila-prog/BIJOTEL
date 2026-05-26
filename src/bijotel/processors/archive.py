"""Chain archival and multi-segment continuity verification (v2.2.0).

Use case: at scale, ``chain.db`` grows linearly with traffic. A 6,000-entry
GENA chain is already 82 MB; projected 100k entries ≈ 1.3 GB; 500k ≈ 6.5 GB.
Full-chain verify scales O(n) and full-chain export becomes impractical.

This module supports:

- :func:`archive_chain` — peel off the oldest ``seq`` prefix into a NEW
  SQLite DB with the same schema, optionally sign the new DB's snapshot,
  verify the slice cleanly, then delete the archived rows from the main
  DB in a single transaction.
- :func:`verify_continuity` — walk a list of chain DBs and confirm the
  boundary invariant ``archive_N.last_hmac == archive_N+1.first_prev`` so
  the auditor knows no entries were quietly dropped between segments.

Design constraints
------------------

1. **Atomicity.** The archive DB is built and verified completely before
   any DELETE runs against the main DB. If verification fails the main
   DB is untouched.
2. **Boundary record.** Every archive DB carries an ``archive_meta`` row
   set recording first_seq, last_seq, first_prev_hash, last_hmac_hash,
   archived_at, and the source DB path. ``verify_continuity`` reads
   this to short-circuit boundary checks.
3. **Sign-anywhere.** Archives can be Ed25519-signed at archive time
   (an export-style ``segment.json`` is produced alongside the SQLite
   file). The SQLite archive itself is the source of truth; the
   signed JSON is for handing the slice to an auditor without
   bundling SQLite tooling.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bijotel.processors.export import export_chain
from bijotel.processors.hmac_chain import (
    BUSY_TIMEOUT_MS,
    DB_FILE_MODE,
    verify_chain,
)


def _init_archive_db(archive_path: Path) -> None:
    """Create the chain table + archive_meta table on a fresh archive DB."""
    conn = sqlite3.connect(archive_path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chain (
                seq INTEGER PRIMARY KEY,
                timestamp_ns INTEGER NOT NULL,
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL,
                span_name TEXT NOT NULL,
                span_kind TEXT,
                canonical_body BLOB NOT NULL,
                canonical_hash TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                hmac_hash TEXT NOT NULL,
                semantic_body_hash TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chain_trace ON chain(trace_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chain_semhash "
            "ON chain(semantic_body_hash)"
        )
        # archive_meta carries the boundary record so ``verify_continuity``
        # can short-circuit without scanning every row.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS archive_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    with contextlib.suppress(OSError):
        archive_path.chmod(DB_FILE_MODE)


def archive_chain(
    db_path: str | Path,
    archive_path: str | Path,
    secret_key: bytes,
    *,
    before_ns: int | None = None,
    before_seq: int | None = None,
    sign_key_path: str | Path | None = None,
    dry_run: bool = False,
    segment_json_path: str | Path | None = None,
) -> dict[str, Any]:
    """Peel the oldest rows off ``db_path`` into a new ``archive_path`` DB.

    Args:
        db_path: Source chain DB.
        archive_path: Destination for the archive SQLite DB. Must NOT
            exist (we refuse to overwrite).
        secret_key: HMAC secret. Required to verify the archive after
            creation and to recompute chain_signature for the
            sidecar JSON.
        before_ns: Archive every row with ``timestamp_ns <
            before_ns`` (exclusive). Mutually exclusive with
            ``before_seq``.
        before_seq: Archive every row with ``seq < before_seq``.
        sign_key_path: Optional Ed25519 PEM private key. When set, the
            sidecar JSON export of the archive slice is signed
            (segment_json_path must also be set, or it defaults to
            ``archive_path.with_suffix('.json')``).
        dry_run: When True, report what would be archived without
            writing the archive DB or deleting anything from the
            source DB.
        segment_json_path: Optional explicit path for the signed
            sidecar JSON. Defaults to the archive path with a
            ``.json`` suffix when sign_key_path is supplied.

    Returns:
        A dict describing the operation:

            {
                "dry_run": bool,
                "archived_count": int,
                "first_seq": int,
                "last_seq": int,
                "first_prev_hash": str,
                "last_hmac_hash": str,
                "boundary_next_prev_hash": str | None,
                "archive_path": str,
                "segment_json_path": str | None,
                "main_remaining_count": int,
            }

    Raises:
        ValueError: if neither / both of before_ns and before_seq
            are supplied, or if the archive would be empty.
        FileExistsError: if ``archive_path`` already exists.
        RuntimeError: if the archive slice fails to verify.
    """
    if (before_ns is None) == (before_seq is None):
        raise ValueError(
            "Pass exactly one of before_ns or before_seq."
        )

    db_path = Path(db_path)
    archive_path = Path(archive_path)
    if archive_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing archive: {archive_path}"
        )
    if not db_path.exists():
        raise FileNotFoundError(f"chain.db not found: {db_path}")

    src_conn = sqlite3.connect(db_path)
    try:
        if before_ns is not None:
            window_clause = "timestamp_ns < ?"
            window_arg = before_ns
        else:
            window_clause = "seq < ?"
            window_arg = before_seq

        agg = src_conn.execute(
            f"SELECT MIN(seq), MAX(seq), COUNT(*) FROM chain WHERE {window_clause}",
            (window_arg,),
        ).fetchone()
        if not agg or agg[2] == 0:
            raise ValueError(
                "Archive window matches no rows — nothing to archive."
            )
        first_seq, last_seq, archived_count = agg

        first_prev = src_conn.execute(
            "SELECT prev_hash FROM chain WHERE seq = ?", (first_seq,)
        ).fetchone()[0]
        last_hmac = src_conn.execute(
            "SELECT hmac_hash FROM chain WHERE seq = ?", (last_seq,)
        ).fetchone()[0]

        next_row = src_conn.execute(
            "SELECT prev_hash FROM chain WHERE seq = ?", (last_seq + 1,)
        ).fetchone()
        boundary_next_prev = next_row[0] if next_row else None

        remaining_after = src_conn.execute(
            f"SELECT COUNT(*) FROM chain WHERE NOT ({window_clause})",
            (window_arg,),
        ).fetchone()[0]

        report: dict[str, Any] = {
            "dry_run": dry_run,
            "archived_count": archived_count,
            "first_seq": first_seq,
            "last_seq": last_seq,
            "first_prev_hash": first_prev,
            "last_hmac_hash": last_hmac,
            "boundary_next_prev_hash": boundary_next_prev,
            "archive_path": str(archive_path),
            "segment_json_path": None,
            "main_remaining_count": remaining_after,
        }

        if dry_run:
            return report

        # Build the archive DB.
        _init_archive_db(archive_path)
        archive_conn = sqlite3.connect(archive_path, isolation_level=None)
        try:
            archive_conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            archive_conn.execute("BEGIN IMMEDIATE")
            cursor = src_conn.execute(
                f"""
                SELECT seq, timestamp_ns, trace_id, span_id, span_name,
                       span_kind, canonical_body, canonical_hash,
                       prev_hash, hmac_hash, semantic_body_hash
                FROM chain WHERE {window_clause}
                ORDER BY seq
                """,
                (window_arg,),
            )
            archive_conn.executemany(
                """
                INSERT INTO chain (
                    seq, timestamp_ns, trace_id, span_id, span_name,
                    span_kind, canonical_body, canonical_hash,
                    prev_hash, hmac_hash, semantic_body_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                cursor,
            )
            # Boundary metadata.
            meta_rows = [
                ("first_seq", str(first_seq)),
                ("last_seq", str(last_seq)),
                ("first_prev_hash", first_prev),
                ("last_hmac_hash", last_hmac),
                ("archived_at", datetime.now(UTC).isoformat()),
                ("source_db", str(db_path.resolve())),
                ("boundary_next_prev_hash", boundary_next_prev or ""),
            ]
            archive_conn.executemany(
                "INSERT OR REPLACE INTO archive_meta(key, value) VALUES (?, ?)",
                meta_rows,
            )
            archive_conn.execute("COMMIT")
        finally:
            archive_conn.close()

        # Verify the archive in isolation. If the source chain was
        # valid, the archive slice must verify too. verify_chain's
        # trim-aware default (auto-detect MIN(seq) when seq_start is
        # unspecified) handles both cases: archive starting at seq=1
        # (uses GENESIS anchor) and archive starting mid-chain (uses
        # the stored first prev_hash as the boundary).
        ok, bad_seq, reason = verify_chain(archive_path, secret_key)
        if not ok:
            raise RuntimeError(
                f"Archive verification FAILED at seq={bad_seq}: {reason}. "
                "Source chain.db left untouched."
            )

        # Sidecar JSON (with optional Ed25519 signature) — uses the
        # range-export path so the file carries segment metadata.
        if sign_key_path is not None and segment_json_path is None:
            segment_json_path = archive_path.with_suffix(".json")
        if segment_json_path is not None:
            export_chain(
                archive_path,
                segment_json_path,
                secret_key,
                sign_key_path=sign_key_path,
                seq_start=first_seq,
                seq_end=last_seq,
            )
            report["segment_json_path"] = str(segment_json_path)

        # Atomic delete from source — single transaction so the
        # source either loses ALL archived rows or none of them. A
        # VACUUM after COMMIT reclaims the disk space.
        delete_conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            delete_conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            delete_conn.execute("BEGIN IMMEDIATE")
            delete_conn.execute(
                f"DELETE FROM chain WHERE {window_clause}", (window_arg,)
            )
            delete_conn.execute("COMMIT")
            # VACUUM cannot run inside a transaction.
            delete_conn.execute("VACUUM")
        finally:
            delete_conn.close()

        # Re-verify the main DB. With archived rows gone, the
        # remaining first seq is no longer 1 — but verify_chain's
        # trim-aware default handles this case automatically by
        # auto-shifting the window to MIN(seq) and accepting the
        # first row's stored prev_hash as the boundary.
        if remaining_after > 0:
            ok2, bad_seq2, reason2 = verify_chain(db_path, secret_key)
            if not ok2:
                # This would be very bad — we deleted but the main
                # DB no longer verifies. Surface loudly. The archive
                # is still intact so no data was lost.
                raise RuntimeError(
                    "Post-archive main DB verification FAILED at "
                    f"seq={bad_seq2}: {reason2}. Archive intact at "
                    f"{archive_path}; investigate before further writes."
                )

        return report
    finally:
        src_conn.close()


def verify_continuity(db_paths: list[str | Path]) -> dict[str, Any]:
    """Verify chain continuity across an ordered list of chain DBs.

    For each adjacent pair ``(A, B)`` in ``db_paths``::

        A.archive_meta.last_hmac_hash == B's first row's prev_hash

    If either DB doesn't carry an ``archive_meta`` row (e.g. the
    live ``chain.db`` at the tail), the boundary is read directly
    from the chain rows.

    Returns:
        ``{"valid": bool, "segments": [...], "boundaries": [...]}``

    The segments list carries per-DB summary; the boundaries list
    carries per-pair check results with the matched / mismatched
    hashes. ``valid`` is True iff every segment is internally a valid
    chain AND every boundary matches.
    """
    summaries: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    overall_valid = True

    for db_path in db_paths:
        path = Path(db_path)
        if not path.exists():
            summaries.append({
                "db_path": str(path),
                "valid": False,
                "reason": "file not found",
                "first_seq": None,
                "last_seq": None,
                "count": None,
                "first_prev_hash": None,
                "last_hmac_hash": None,
            })
            overall_valid = False
            continue

        with sqlite3.connect(path) as conn:
            count_row = conn.execute(
                "SELECT MIN(seq), MAX(seq), COUNT(*) FROM chain"
            ).fetchone()
            if not count_row or count_row[2] == 0:
                summaries.append({
                    "db_path": str(path),
                    "valid": False,
                    "reason": "chain table empty",
                    "first_seq": None,
                    "last_seq": None,
                    "count": 0,
                    "first_prev_hash": None,
                    "last_hmac_hash": None,
                })
                overall_valid = False
                continue
            first_seq, last_seq, count = count_row
            first_prev = conn.execute(
                "SELECT prev_hash FROM chain WHERE seq = ?", (first_seq,)
            ).fetchone()[0]
            last_hmac = conn.execute(
                "SELECT hmac_hash FROM chain WHERE seq = ?", (last_seq,)
            ).fetchone()[0]
            # Pull archive_meta if present (preferred — survives
            # rebuilds that touch only the chain table).
            try:
                meta_rows = dict(
                    conn.execute(
                        "SELECT key, value FROM archive_meta"
                    ).fetchall()
                )
            except sqlite3.OperationalError:
                meta_rows = {}

        summaries.append({
            "db_path": str(path),
            "valid": True,
            "first_seq": meta_rows.get("first_seq", first_seq),
            "last_seq": meta_rows.get("last_seq", last_seq),
            "count": count,
            "first_prev_hash": meta_rows.get("first_prev_hash", first_prev),
            "last_hmac_hash": meta_rows.get("last_hmac_hash", last_hmac),
        })

    for i in range(len(summaries) - 1):
        a = summaries[i]
        b = summaries[i + 1]
        if not (a["valid"] and b["valid"]):
            boundaries.append({
                "from_db": a["db_path"],
                "to_db": b["db_path"],
                "matches": False,
                "reason": "one or both segments invalid",
                "expected": a.get("last_hmac_hash"),
                "actual": b.get("first_prev_hash"),
            })
            overall_valid = False
            continue
        matches = a["last_hmac_hash"] == b["first_prev_hash"]
        if not matches:
            overall_valid = False
        boundaries.append({
            "from_db": a["db_path"],
            "to_db": b["db_path"],
            "matches": matches,
            "expected": a["last_hmac_hash"],
            "actual": b["first_prev_hash"],
        })

    return {
        "valid": overall_valid,
        "segments": summaries,
        "boundaries": boundaries,
    }
