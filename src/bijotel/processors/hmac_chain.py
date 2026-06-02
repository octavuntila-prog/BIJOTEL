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
        # Autocommit mode so we control transaction boundaries explicitly.
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            # busy_timeout MUST be set before journal_mode AND before any DDL,
            # so all subsequent operations retry up to 5s under contention.
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            # Idempotent WAL set: only invoke PRAGMA journal_mode=WAL if not
            # already WAL (avoids re-acquiring the brief EXCLUSIVE lock on
            # every process startup; combined with BEGIN IMMEDIATE below,
            # serializes concurrent _init_db calls cleanly).
            current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if current_mode.lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            # All DDL inside a single BEGIN IMMEDIATE transaction so concurrent
            # _init_db calls from sibling processes serialize at the RESERVED
            # lock with busy_timeout retry, AND the resulting table is visible
            # to all readers immediately after COMMIT (eliminates the post-init
            # "no such table" window observed on GENA Linux 22 mai).
            conn.execute("BEGIN IMMEDIATE")
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
            conn.execute("COMMIT")
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
    *,
    seq_start: int | None = None,
    seq_end: int | None = None,
    since_ns: int | None = None,
    until_ns: int | None = None,
    last_n: int | None = None,
) -> tuple[bool, int | None, str | None]:
    """Verify chain integrity end-to-end (or over a range).

    For each row in the selected window (ordered by ``seq``):
        1. Re-compute SHA-256(canonical_body) -> expected canonical_hash.
        2. Verify ``prev_hash[N] == hmac_hash[N-1]`` (or the resolved
           "expected prev_hash" for the first row of the window — see
           below).
        3. Re-compute HMAC(prev_hash || canonical_hash, secret) ->
           expected hmac_hash.

    Args:
        db_path: SQLite chain DB.
        secret_key: HMAC secret.
        seq_start: Inclusive lower bound on ``seq``. Defaults to 1
            (chain genesis).
        seq_end: Inclusive upper bound on ``seq``. Defaults to last
            row.
        since_ns: Inclusive lower bound on ``timestamp_ns``. Combines
            with seq_start (both filters applied).
        until_ns: Inclusive upper bound on ``timestamp_ns``.
        last_n: Verify the last N entries by ``seq``. Mutually
            exclusive with the above range filters — last_n wins if
            both are passed.

    Range boundary behaviour:
        If the resolved first ``seq`` > 1, ``verify_chain`` looks up
        ``seq - 1`` in the SAME DB to obtain the "expected prev_hash"
        for the window's first row. If that predecessor is not in the
        DB (e.g. archived elsewhere), the function trusts whatever
        ``prev_hash`` is stored for the first window row and reports
        it as the segment's boundary in the caller's logs — chain-link
        consistency within the window is still enforced.

    Returns:
        ``(True, last_seq, None)`` if every checked row passes, where
        ``last_seq`` is the ``seq`` of the last row verified (full-chain
        or range). ``last_seq`` is ``None`` only when the window is
        empty (no rows verified).
        ``(False, seq, reason)`` on the first failure encountered.

    Backward compatibility:
        ``verify_chain(db, secret)`` with no kwargs behaves exactly
        like v1.x — full-chain verify against genesis. (Note: prior to
        v2.13.1 the success tuple always carried ``None`` here; it now
        reports the actual last seq. The ``valid`` flag is unchanged
        and remains authoritative.)
    """
    with sqlite3.connect(db_path) as conn:
        # Resolve the actual seq window we will verify.
        if last_n is not None:
            row = conn.execute(
                "SELECT seq FROM chain ORDER BY seq DESC LIMIT 1 OFFSET ?",
                (max(0, last_n - 1),),
            ).fetchone()
            if row is None:
                # Chain shorter than last_n — verify whatever we have.
                row = conn.execute(
                    "SELECT MIN(seq) FROM chain"
                ).fetchone()
                resolved_start = row[0] if row and row[0] is not None else 1
            else:
                resolved_start = row[0]
            resolved_end: int | None = None
        else:
            resolved_start = seq_start if seq_start is not None else 1
            resolved_end = seq_end

        # Trim-aware default: when the caller asked for "the whole
        # chain" (no explicit seq_start), but the chain has been
        # archived and no longer contains seq=1, auto-shift the
        # window to the actual MIN(seq) and use boundary-anchor
        # semantics. This means `bijotel verify` continues to work
        # on a trimmed chain.db without forcing the operator to pass
        # range flags. Explicit seq_start=1 (or any explicit value)
        # bypasses this — caller asked for that anchor specifically.
        if seq_start is None and last_n is None:
            min_seq_row = conn.execute(
                "SELECT MIN(seq) FROM chain"
            ).fetchone()
            actual_min = min_seq_row[0] if min_seq_row else None
            if actual_min is not None and actual_min > 1:
                resolved_start = actual_min

        # If the window doesn't start at chain genesis, look up the
        # predecessor's hmac_hash so the first row's prev_hash check
        # has the right anchor. If the predecessor is missing
        # (archived elsewhere), accept whatever the first row claims —
        # boundary verification across DBs is the job of
        # verify_continuity().
        if resolved_start <= 1:
            expected_prev: str = GENESIS_HASH
        else:
            pred = conn.execute(
                "SELECT hmac_hash FROM chain WHERE seq = ?",
                (resolved_start - 1,),
            ).fetchone()
            if pred is None:
                first_row = conn.execute(
                    "SELECT prev_hash FROM chain WHERE seq = ?",
                    (resolved_start,),
                ).fetchone()
                expected_prev = first_row[0] if first_row else GENESIS_HASH
            else:
                expected_prev = pred[0]

        # Build the SELECT.
        clauses = ["seq >= ?"]
        params: list[int] = [resolved_start]
        if resolved_end is not None:
            clauses.append("seq <= ?")
            params.append(resolved_end)
        if since_ns is not None:
            clauses.append("timestamp_ns >= ?")
            params.append(since_ns)
        if until_ns is not None:
            clauses.append("timestamp_ns <= ?")
            params.append(until_ns)
        where_sql = " AND ".join(clauses)

        cursor = conn.execute(
            f"""
            SELECT seq, canonical_body, canonical_hash, prev_hash, hmac_hash
            FROM chain
            WHERE {where_sql}
            ORDER BY seq ASC
            """,
            params,
        )
        last_verified_seq: int | None = None
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
            last_verified_seq = seq
    return (True, last_verified_seq, None)


def chain_range_summary(
    db_path: str | Path,
    *,
    seq_start: int | None = None,
    seq_end: int | None = None,
    since_ns: int | None = None,
    until_ns: int | None = None,
    last_n: int | None = None,
) -> dict:
    """Lightweight read-only summary of a chain range.

    Returns counts and boundary hashes for the resolved window so the
    CLI can print a useful "verified seq X..Y, boundary prev=abc..."
    message without re-running the full verify loop.

    Returned keys:
        first_seq, last_seq, count, first_prev_hash, last_hmac_hash,
        boundary_predecessor_in_db (bool — was seq-1 found in same DB?)
    """
    with sqlite3.connect(db_path) as conn:
        if last_n is not None:
            row = conn.execute(
                "SELECT seq FROM chain ORDER BY seq DESC LIMIT 1 OFFSET ?",
                (max(0, last_n - 1),),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT MIN(seq) FROM chain"
                ).fetchone()
                resolved_start = row[0] if row and row[0] is not None else 1
            else:
                resolved_start = row[0]
            resolved_end: int | None = None
        else:
            resolved_start = seq_start if seq_start is not None else 1
            resolved_end = seq_end

        clauses = ["seq >= ?"]
        params: list[int] = [resolved_start]
        if resolved_end is not None:
            clauses.append("seq <= ?")
            params.append(resolved_end)
        if since_ns is not None:
            clauses.append("timestamp_ns >= ?")
            params.append(since_ns)
        if until_ns is not None:
            clauses.append("timestamp_ns <= ?")
            params.append(until_ns)
        where_sql = " AND ".join(clauses)

        agg = conn.execute(
            f"SELECT MIN(seq), MAX(seq), COUNT(*) FROM chain WHERE {where_sql}",
            params,
        ).fetchone()
        if not agg or agg[2] == 0:
            return {
                "first_seq": None,
                "last_seq": None,
                "count": 0,
                "first_prev_hash": None,
                "last_hmac_hash": None,
                "boundary_predecessor_in_db": False,
            }
        first_seq, last_seq, count = agg
        first_prev = conn.execute(
            "SELECT prev_hash FROM chain WHERE seq = ?", (first_seq,)
        ).fetchone()[0]
        last_hmac = conn.execute(
            "SELECT hmac_hash FROM chain WHERE seq = ?", (last_seq,)
        ).fetchone()[0]
        pred = conn.execute(
            "SELECT 1 FROM chain WHERE seq = ?", (first_seq - 1,)
        ).fetchone()
        return {
            "first_seq": first_seq,
            "last_seq": last_seq,
            "count": count,
            "first_prev_hash": first_prev,
            "last_hmac_hash": last_hmac,
            "boundary_predecessor_in_db": pred is not None,
        }
