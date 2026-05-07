"""HmacChainSpanProcessor: tamper-evident audit chain peste GenAI spans."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from bijotel.processors.canonical import canonicalize, span_to_canonical_dict

GENESIS_HASH = "0" * 64  # Convenție: prev_hash al primului entry


def _default_filter(span: ReadableSpan) -> bool:
    """Default: keep spans cu cel puțin un atribut gen_ai.*."""
    if not span.attributes:
        return False
    return any(key.startswith("gen_ai.") for key in span.attributes)


class HmacChainSpanProcessor(SpanProcessor):
    """SpanProcessor care sigilează spans într-un HMAC chain SQLite.

    Pentru fiecare span care trece filter-ul:
        1. Extract canonical dict (span_to_canonical_dict)
        2. JCS canonicalize -> bytes
        3. SHA-256 -> canonical_hash
        4. HMAC(prev_hash || canonical_hash, secret) -> hmac_hash
        5. INSERT row în chain table

    Chain integrity: pentru orice seq N, hmac_hash[N] depinde de hmac_hash[N-1].
    Mutația oricărui field în orice row -> verify() returnează (False, seq).
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
        """Create chain table if not exists."""
        with sqlite3.connect(self._db_path) as conn:
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
                    hmac_hash TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chain_trace ON chain(trace_id)"
            )
            conn.commit()

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
        """Sigilează span în chain dacă trece filter-ul."""
        if not self._filter(span):
            return

        canonical_dict = span_to_canonical_dict(span)
        canonical_body = canonicalize(canonical_dict)
        canonical_hash = hashlib.sha256(canonical_body).hexdigest()

        with self._lock, sqlite3.connect(self._db_path) as conn:
            prev_hash = self._last_hmac_hash(conn)
            hmac_input = (prev_hash + canonical_hash).encode("utf-8")
            hmac_hash = hmac.new(self._secret, hmac_input, hashlib.sha256).hexdigest()

            trace_id_hex = format(span.context.trace_id, "032x")
            span_id_hex = format(span.context.span_id, "016x")

            conn.execute(
                """
                INSERT INTO chain (
                    timestamp_ns, trace_id, span_id, span_name, span_kind,
                    canonical_body, canonical_hash, prev_hash, hmac_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            conn.commit()

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
