"""CasSpanProcessor: content-addressable storage cu dedup pe input-only body."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from bijotel.processors.canonical import canonicalize, span_to_semantic_dict

BUSY_TIMEOUT_MS = 5000  # SQLite per-connection retry budget under contention
DB_FILE_MODE = 0o600    # Owner read/write only; cas.body holds prompt BLOBs

_LOG = logging.getLogger("bijotel.cas")


def _default_filter(span: ReadableSpan) -> bool:
    """Default: keep spans cu cel puțin un atribut gen_ai.*."""
    if not span.attributes:
        return False
    return any(key.startswith("gen_ai.") for key in span.attributes)


class CasSpanProcessor(SpanProcessor):
    """SpanProcessor care stochează input-only span body în content-addressable store.

    Pentru fiecare span care trece filter-ul:
        1. Extract semantic dict (exclude output, usage, response, IDs, timestamps)
        2. JCS canonicalize -> bytes
        3. SHA-256 -> body_hash
        4. INSERT INTO cas ... ON CONFLICT(body_hash) DO UPDATE SET ref_count += 1

    Two calls cu input identic -> același body_hash -> ref_count crește.
    Two calls cu input diferit -> body_hash diferit -> entries separate.
    Output, timing, response IDs NU influențează body_hash (input-only dedup).

    Cross-reference: chain.semantic_body_hash referă cas.body_hash.
    Pentru "list spans using body X": query chain WHERE semantic_body_hash = X.

    NOTE: Same DB ca HmacChainSpanProcessor recomandat (single file backup,
    common SQLite lock). NU există cross-processor atomicity — separate INSERTs
    în separate transactions. Dacă crash între chain INSERT și cas INSERT,
    state e inconsistent (un INSERT a reușit, celălalt nu).
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        filter_fn: Callable[[ReadableSpan], bool] | None = None,
    ) -> None:
        """Initialize CasSpanProcessor.

        Args:
            db_path: Path către SQLite file (creat dacă nu există).
                Same path ca HmacChainSpanProcessor -> same DB (recomandat).
            filter_fn: Span filter. Default: keep spans cu gen_ai.* attrs.
        """
        self._filter = filter_fn or _default_filter
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create cas table if not exists, enable WAL, apply restrictive perms.

        WAL + ``busy_timeout`` are idempotent at the database level; safe to set
        even if ``HmacChainSpanProcessor`` shares the same db_path and already
        configured them. ``DB_FILE_MODE`` (0o600) applied only to newly-created
        files; existing perms preserved.
        """
        db_existed = self._db_path.exists()
        # Autocommit + busy_timeout + BEGIN IMMEDIATE for DDL serialization
        # across concurrent processes (see hmac_chain.py _init_db for full
        # rationale; same fix applied here for shared-db consistency).
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if current_mode.lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cas (
                    body_hash TEXT PRIMARY KEY,
                    body BLOB NOT NULL,
                    first_seen_ns INTEGER NOT NULL,
                    ref_count INTEGER NOT NULL DEFAULT 1
                )
            """)
            conn.execute("COMMIT")
        finally:
            conn.close()

        # Best-effort: Windows / some filesystems lack POSIX chmod semantics.
        if not db_existed and self._db_path.exists():
            with contextlib.suppress(OSError):
                self._db_path.chmod(DB_FILE_MODE)

    def _connect_for_write(self) -> sqlite3.Connection:
        """Open a write-mode connection with explicit transaction control."""
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        """SpanProcessor interface: no-op pe start."""
        pass

    def on_end(self, span: ReadableSpan) -> None:
        """Store input-only semantic body în CAS, increment ref_count on duplicate.

        Crash-isolated: any exception during canonicalization, hashing, or
        SQLite write is caught, logged at ERROR level, and SUPPRESSED. The
        host application's LLM call path is never disturbed by CAS-write
        failures. A missed entry just means dedup is one-short for that body;
        subsequent identical inputs still bump ref_count.

        Single-statement INSERT + ON CONFLICT DO UPDATE is atomic in SQLite,
        but ``BEGIN IMMEDIATE`` is used for consistent multi-writer semantics
        with HmacChainSpanProcessor (same db, same lock model).
        """
        try:
            if not self._filter(span):
                return

            semantic_dict = span_to_semantic_dict(span)
            semantic_body = canonicalize(semantic_dict)
            body_hash = hashlib.sha256(semantic_body).hexdigest()

            with self._lock:
                conn = self._connect_for_write()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        INSERT INTO cas (body_hash, body, first_seen_ns, ref_count)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(body_hash) DO UPDATE SET ref_count = ref_count + 1
                        """,
                        (body_hash, semantic_body, span.end_time),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    # Best-effort rollback; OperationalError = no active txn
                    with contextlib.suppress(sqlite3.OperationalError):
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
        except Exception as e:
            _LOG.error(
                "bijotel cas write failed (body not stored, host unaffected): "
                "%s: %s",
                type(e).__name__,
                e,
            )

    def shutdown(self) -> None:
        """SpanProcessor interface: no-op (SQLite connection per call)."""
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """SpanProcessor interface: no-op (writes sunt sync în on_end)."""
        return True


def cas_lookup(
    db_path: str | Path,
    body_hash: str,
) -> tuple[bytes, int, int] | None:
    """Lookup CAS entry by hash.

    Returns:
        (body, first_seen_ns, ref_count) or None if not found.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT body, first_seen_ns, ref_count FROM cas WHERE body_hash = ?",
            (body_hash,),
        ).fetchone()
        return tuple(row) if row else None


def cas_stats(db_path: str | Path) -> dict[str, float]:
    """Return CAS stats: unique_bodies, total_refs, dedup_factor."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(ref_count), 0) FROM cas"
        ).fetchone()
        unique = row[0]
        total = row[1]
        return {
            "unique_bodies": unique,
            "total_refs": total,
            "dedup_factor": total / unique if unique > 0 else 0,
        }
