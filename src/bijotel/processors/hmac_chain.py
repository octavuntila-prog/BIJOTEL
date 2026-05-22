"""HmacChainSpanProcessor: tamper-evident audit chain peste GenAI spans."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from bijotel.processors.canonical import (
    canonicalize,
    span_to_canonical_dict,
    span_to_semantic_dict,
)

GENESIS_HASH = "0" * 64  # Convenție: prev_hash al primului entry
BUSY_TIMEOUT_MS = 5000  # SQLite per-connection retry budget under contention
DB_FILE_MODE = 0o600    # Owner read/write only; chain.canonical_body holds prompt BLOBs

_LOG = logging.getLogger("bijotel.chain")


def _default_filter(span: ReadableSpan) -> bool:
    """Default: keep spans cu cel puțin un atribut gen_ai.*."""
    if not span.attributes:
        return False
    return any(key.startswith("gen_ai.") for key in span.attributes)


class HmacChainSpanProcessor(SpanProcessor):
    """SpanProcessor care sigilează spans într-un HMAC chain SQLite.

    Pentru fiecare span care trece filter-ul:
        1. Extract canonical dict (span_to_canonical_dict) -> JCS -> SHA-256 -> canonical_hash
        2. Extract semantic dict (input-only) -> JCS -> SHA-256 -> semantic_body_hash
        3. HMAC(prev_hash || canonical_hash, secret) -> hmac_hash
        4. INSERT row în chain table (cu both hashes populated)

    Chain integrity: pentru orice seq N, hmac_hash[N] depinde de hmac_hash[N-1].
    Mutația oricărui field în orice row -> verify() returnează (False, seq).

    Cross-reference: chain.semantic_body_hash referă cas.body_hash (vezi cas.py).
    Pentru "list spans using body X": query chain WHERE semantic_body_hash = X.

    Schema migration: dacă chain.db existent NU are coloana semantic_body_hash,
    _init_db() o adaugă via ALTER TABLE (idempotent, non-destructive). Existing
    rows rămân cu NULL pentru semantic_body_hash; new rows se populează.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        secret_key: bytes,
        filter_fn: Callable[[ReadableSpan], bool] | None = None,
    ) -> None:
        """Initialize HmacChainSpanProcessor.

        Args:
            db_path: Path către SQLite file (creat dacă nu există).
            secret_key: HMAC secret bytes. Min 16 bytes recommended.
            filter_fn: Span filter. Default: keep spans cu gen_ai.* attrs.
        """
        if len(secret_key) < 16:
            raise ValueError("secret_key must be at least 16 bytes")
        self._secret = secret_key
        self._filter = filter_fn or _default_filter
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create chain table if not exists, enable WAL, migrate older schema if needed.

        WAL mode (``PRAGMA journal_mode=WAL``) is set once and persists at the
        database level, enabling safe concurrent reads + serialized writes.
        Combined with ``BEGIN IMMEDIATE`` in ``on_end``, this gives multi-writer
        correctness without per-process lock coordination.

        ``DB_FILE_MODE`` (0o600) is applied only to newly-created files;
        existing chain.db permissions are preserved (M5 nothing-deleted).
        Best-effort: silently skipped on platforms without chmod semantics
        (Windows, some special filesystems).
        """
        db_existed = self._db_path.exists()
        conn = sqlite3.connect(self._db_path)
        try:
            # WAL persists at database level; safe to set repeatedly
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chain (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
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
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chain_trace ON chain(trace_id)"
            )
            # Migration: add semantic_body_hash to existing chain tables (F2 -> F3 upgrade)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(chain)").fetchall()]
            if "semantic_body_hash" not in cols:
                conn.execute("ALTER TABLE chain ADD COLUMN semantic_body_hash TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chain_semhash "
                "ON chain(semantic_body_hash)"
            )
            conn.commit()
        finally:
            conn.close()

        # Apply restrictive perms only on newly-created files; never alter existing.
        # Best-effort: Windows / some filesystems lack POSIX chmod semantics.
        if not db_existed and self._db_path.exists():
            with contextlib.suppress(OSError):
                self._db_path.chmod(DB_FILE_MODE)

    def _connect_for_write(self) -> sqlite3.Connection:
        """Open a write-mode connection with explicit transaction control.

        Uses ``isolation_level=None`` (autocommit) so ``BEGIN IMMEDIATE`` can be
        issued explicitly before the SELECT-then-INSERT critical section.
        Sets ``busy_timeout`` so concurrent writers wait up to 5s rather than
        immediately raising ``SQLITE_BUSY``.
        """
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    def _last_hmac_hash(self, conn: sqlite3.Connection) -> str:
        """Return hmac_hash al ultimului entry, sau GENESIS_HASH dacă chain e gol."""
        row = conn.execute(
            "SELECT hmac_hash FROM chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS_HASH

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        """SpanProcessor interface: no-op pe start (sigilăm la on_end)."""
        pass

    def on_end(self, span: ReadableSpan) -> None:
        """Sigilează span în chain dacă trece filter-ul.

        Crash-isolated: any exception during canonicalization, hashing, or
        SQLite write is caught, logged at ERROR level, and SUPPRESSED. The
        host application's LLM call path is never disturbed by chain-write
        failures. The chain may have a gap (one missing entry); subsequent
        entries continue normally with the still-valid ``prev_hash`` of the
        last successfully sealed row.

        Multi-writer safe: the SELECT-prev-hash → compute → INSERT critical
        section is wrapped in ``BEGIN IMMEDIATE`` so the RESERVED lock is
        acquired before the SELECT, eliminating the read-modify-write race
        across concurrent processes sharing the same chain.db.
        """
        try:
            if not self._filter(span):
                return

            canonical_dict = span_to_canonical_dict(span)
            canonical_body = canonicalize(canonical_dict)
            canonical_hash = hashlib.sha256(canonical_body).hexdigest()

            semantic_dict = span_to_semantic_dict(span)
            semantic_body = canonicalize(semantic_dict)
            semantic_hash = hashlib.sha256(semantic_body).hexdigest()

            with self._lock:
                conn = self._connect_for_write()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    prev_hash = self._last_hmac_hash(conn)
                    hmac_input = (prev_hash + canonical_hash).encode("utf-8")
                    hmac_hash = hmac.new(
                        self._secret, hmac_input, hashlib.sha256
                    ).hexdigest()

                    trace_id_hex = format(span.context.trace_id, "032x")
                    span_id_hex = format(span.context.span_id, "016x")

                    conn.execute(
                        """
                        INSERT INTO chain (
                            timestamp_ns, trace_id, span_id, span_name, span_kind,
                            canonical_body, canonical_hash, prev_hash, hmac_hash,
                            semantic_body_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            span.end_time,
                            trace_id_hex,
                            span_id_hex,
                            span.name,
                            span.kind.name if span.kind else None,
                            canonical_body,
                            canonical_hash,
                            prev_hash,
                            hmac_hash,
                            semantic_hash,
                        ),
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
                "bijotel chain write failed (span not chained, host unaffected): "
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


def verify_chain(
    db_path: str | Path,
    secret_key: bytes,
) -> tuple[bool, int | None, str | None]:
    """Verify chain integrity end-to-end.

    For fiecare row în chain (ordinea seq):
        1. Re-compute SHA-256(canonical_body) -> expected canonical_hash
        2. Re-compute HMAC(prev_hash || canonical_hash, secret) -> expected hmac_hash
        3. Verify prev_hash[N] == hmac_hash[N-1] (sau GENESIS pentru N=1)

    Returns:
        (True, None, None) dacă chain valid.
        (False, seq, reason) dacă mutație detectată la rândul `seq`.
    """
    expected_prev = GENESIS_HASH
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute("""
            SELECT seq, canonical_body, canonical_hash, prev_hash, hmac_hash
            FROM chain ORDER BY seq ASC
        """)
        for seq, body, stored_canonical, stored_prev, stored_hmac in cursor:
            recomputed_canonical = hashlib.sha256(body).hexdigest()
            if recomputed_canonical != stored_canonical:
                return (False, seq, "canonical_hash mismatch (body mutated)")
            if stored_prev != expected_prev:
                return (False, seq, "prev_hash mismatch (chain broken)")
            hmac_input = (stored_prev + stored_canonical).encode("utf-8")
            recomputed_hmac = hmac.new(
                secret_key, hmac_input, hashlib.sha256
            ).hexdigest()
            if recomputed_hmac != stored_hmac:
                return (False, seq, "hmac_hash mismatch (secret wrong or hmac mutated)")
            expected_prev = stored_hmac
    return (True, None, None)
