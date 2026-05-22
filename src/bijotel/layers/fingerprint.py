"""F13 / Bijuteria #7: Semantic fingerprinting for chain entries.

Two fingerprinter implementations with identical interface:

* :class:`DeterministicFingerprinter` — SHA-256 expansion to a 384-dim L2-
  normalized vector. No ML dependency. Reproducible, CI-friendly, fast.
  Useful as fallback when sentence-transformers is not installed, AND as
  the default for testing / determinism contracts.
* :class:`SemanticFingerprinter` — real sentence embeddings via
  ``sentence-transformers`` (``all-MiniLM-L6-v2``, 384-dim normalized).
  Optional dep: ``pip install bijotel[fingerprint]``.

:class:`FingerprintSpanProcessor` is the SpanProcessor wrapper: for each
filtered span, extract text from ``canonical_body``, fingerprint it, and
persist into a SQLite ``fingerprints`` table. Crash-isolated like the
core processors (errors logged to ``bijotel.fingerprint``, suppressed).
Multi-writer-safe via the same WAL + BEGIN IMMEDIATE pattern.

:func:`similarity_search` queries the store for spans whose fingerprint
matches a query string above a similarity threshold.

Use cases:

* Dedup detection beyond input-exact (semantic near-duplicates)
* Copyright similarity scanning (compare new prompts against known
  protected content)
* Content attribution (which earlier prompt influenced this one)

Provenance: the two fingerprinter classes are harvested with minor
adaptation from
`substrate-guard.comply.fingerprinter <https://github.com/octavuntila-prog/substrate-guard>`_
(MIT, Aisophical SRL, same author). The SpanProcessor wrapper and Store
are BIJOTEL-original.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

BUSY_TIMEOUT_MS = 5000
DB_FILE_MODE = 0o600

_LOG = logging.getLogger("bijotel.fingerprint")


# ============================================================================
# Fingerprinter implementations (harvested from substrate-guard.comply)
# ============================================================================


class DeterministicFingerprinter:
    """384-dim L2-normalized embedding via SHA-256 expansion (no ML, reproducible).

    For each dimension ``i``, the value is derived from ``sha256(document|i)``
    first 4 bytes mapped to ``[-1, 1]``. The resulting vector is L2-normalized.

    Stable across processes, OS, Python versions. Useful as: (a) deterministic
    test oracle, (b) fallback when sentence-transformers is not installed,
    (c) similarity for exact-text-matching (full string identity → similarity
    1.0; any byte difference → similarity 0.0-ish since SHA-256 is collision-
    resistant). For semantic near-duplicate detection use
    :class:`SemanticFingerprinter` instead.
    """

    ENCODER = "deterministic-sha256-v1"
    DIMENSIONS = 384

    def fingerprint(self, document: str) -> np.ndarray:
        """Return 384-dim L2-normalized float32 embedding of ``document``."""
        raw = np.empty(self.DIMENSIONS, dtype=np.float64)
        for i in range(self.DIMENSIONS):
            h = hashlib.sha256(f"{document}|{i}".encode()).digest()
            v = int.from_bytes(h[:4], "big") / (2**32) * 2.0 - 1.0
            raw[i] = v
        n = float(np.linalg.norm(raw))
        if n < 1e-12:
            out = np.ones(self.DIMENSIONS, dtype=np.float64) / np.sqrt(self.DIMENSIONS)
        else:
            out = raw / n
        return out.astype(np.float32)

    def fingerprint_batch(
        self, documents: list[str], batch_size: int = 32  # noqa: ARG002
    ) -> np.ndarray:
        """Stack fingerprints for a list of documents. ``batch_size`` is API-compat only."""
        return np.stack([self.fingerprint(d) for d in documents], axis=0)

    def similarity(self, emb_a: np.ndarray, emb_b: np.ndarray) -> float:
        """Cosine similarity (dot product since L2-normalized). Float in [-1, 1]."""
        return float(
            np.dot(np.asarray(emb_a, dtype=np.float64), np.asarray(emb_b, dtype=np.float64))
        )

    def document_hash(self, document: str) -> str:
        """SHA-256 hex of the raw document bytes."""
        return hashlib.sha256(document.encode("utf-8")).hexdigest()

    @property
    def protocol_id(self) -> str:
        """Encoder identity string; persisted with each fingerprint for compatibility checks."""
        return f"det:{self.ENCODER}:dim{self.DIMENSIONS}:l2norm"


class SemanticFingerprinter:
    """sentence-transformers ``all-MiniLM-L6-v2`` (384-dim L2-normalized).

    Real semantic embeddings: similar meanings → high cosine similarity even
    when surface form differs.

    Optional dependency: ``pip install bijotel[fingerprint]`` (pulls
    ``sentence-transformers``). Without the extra, instantiation succeeds
    but ``fingerprint()`` raises :class:`ImportError` on first call (lazy
    model load).
    """

    ENCODER = "all-MiniLM-L6-v2"
    DIMENSIONS = 384

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or self.ENCODER
        self._model = None

    def _get_model(self):  # noqa: ANN202 (sentence_transformers may not be installed)
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:
                raise ImportError(
                    "SemanticFingerprinter requires sentence-transformers. "
                    "Install with: pip install bijotel[fingerprint]"
                ) from e
            self._model = SentenceTransformer(self._model_name)
        return self._model

    def fingerprint(self, document: str) -> np.ndarray:
        model = self._get_model()
        embedding = model.encode(document, normalize_embeddings=True)
        return np.asarray(embedding, dtype=np.float32)

    def fingerprint_batch(
        self, documents: list[str], batch_size: int = 32
    ) -> np.ndarray:
        model = self._get_model()
        embeddings = model.encode(
            documents,
            normalize_embeddings=True,
            batch_size=batch_size,
            show_progress_bar=len(documents) > 100,
        )
        return np.asarray(embeddings, dtype=np.float32)

    def similarity(self, emb_a: np.ndarray, emb_b: np.ndarray) -> float:
        return float(np.dot(np.asarray(emb_a), np.asarray(emb_b)))

    def document_hash(self, document: str) -> str:
        return hashlib.sha256(document.encode("utf-8")).hexdigest()

    @property
    def protocol_id(self) -> str:
        return f"sbert:{self._model_name}:dim{self.DIMENSIONS}:normalized"


# ============================================================================
# Helpers
# ============================================================================


def _default_filter(span: ReadableSpan) -> bool:
    """Default: only spans carrying gen_ai.* attributes (LLM calls)."""
    if not span.attributes:
        return False
    return any(key.startswith("gen_ai.") for key in span.attributes)


def _extract_text(span: ReadableSpan) -> str:
    """Concatenate the user-message text from gen_ai.input.messages.

    Handles both Anthropic-multipart (parts=[{type,text}]) and OpenAI-string
    (content=str) shapes, plus the OTel semantic-convention shape
    (parts=[{content, type}]).
    """
    if not span.attributes:
        return ""
    raw = span.attributes.get("gen_ai.input.messages")
    if raw is None:
        return ""
    if isinstance(raw, str):
        try:
            msgs = json.loads(raw)
        except Exception:
            return raw
    else:
        msgs = raw

    parts: list[str] = []
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict):
                        t = p.get("text") or p.get("content") or ""
                        if isinstance(t, str):
                            parts.append(t)
            ps = m.get("parts")
            if isinstance(ps, list):
                for p in ps:
                    if isinstance(p, dict):
                        t = p.get("content") or p.get("text") or ""
                        if isinstance(t, str):
                            parts.append(t)
    return " ".join(p for p in parts if p).strip()


# ============================================================================
# Store + SpanProcessor
# ============================================================================


class FingerprintSpanProcessor(SpanProcessor):
    """SpanProcessor that fingerprints each filtered span and persists into SQLite.

    Schema (created idempotently in ``_init_db``)::

        fingerprints (
            span_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            encoder TEXT NOT NULL,        -- e.g. "det:..." or "sbert:..."
            embedding BLOB NOT NULL,      -- float32 little-endian, len = DIMENSIONS * 4
            doc_hash TEXT NOT NULL,       -- sha256 of input (cross-ref chain.sem_body_hash)
            created_ns INTEGER NOT NULL
        )

    Same hardening as core processors: WAL + busy_timeout + DDL in
    BEGIN IMMEDIATE; crash-isolated on_end; per-process threading.Lock as
    in-process defense-in-depth.

    Args:
        db_path: Path to SQLite file. Same db as chain.db is recommended
            (single-file backup, unified audit story).
        fingerprinter: Instance providing ``fingerprint(text)``,
            ``similarity(a, b)``, ``document_hash(text)``, ``protocol_id``.
            Defaults to :class:`DeterministicFingerprinter` (no extra deps).
        filter_fn: Span filter. Default: keep spans with gen_ai.* attrs.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        fingerprinter: DeterministicFingerprinter | SemanticFingerprinter | None = None,
        filter_fn: Callable[[ReadableSpan], bool] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._fp = fingerprinter or DeterministicFingerprinter()
        self._filter = filter_fn or _default_filter
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Create fingerprints table; WAL+busy_timeout; DDL in BEGIN IMMEDIATE.

        Same pattern as :class:`HmacChainSpanProcessor._init_db`: see that
        docstring for the multi-process race rationale.
        """
        db_existed = self._db_path.exists()
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        try:
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            current_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if current_mode.lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fingerprints (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    encoder TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    doc_hash TEXT NOT NULL,
                    created_ns INTEGER NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fp_trace ON fingerprints(trace_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fp_dochash ON fingerprints(doc_hash)"
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

        if not db_existed and self._db_path.exists():
            with contextlib.suppress(OSError):
                self._db_path.chmod(DB_FILE_MODE)

    def _connect_for_write(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return conn

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:  # noqa: ARG002
        """SpanProcessor interface: no-op."""

    def on_end(self, span: ReadableSpan) -> None:
        """Fingerprint span text + persist. Crash-isolated."""
        try:
            if not self._filter(span):
                return

            text = _extract_text(span)
            if not text:
                return  # nothing fingerprintable

            emb = self._fp.fingerprint(text)
            emb_blob = emb.astype(np.float32).tobytes()
            doc_hash = self._fp.document_hash(text)
            encoder = self._fp.protocol_id

            trace_id_hex = format(span.context.trace_id, "032x")
            span_id_hex = format(span.context.span_id, "016x")

            with self._lock:
                conn = self._connect_for_write()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        """
                        INSERT INTO fingerprints (
                            span_id, trace_id, encoder, embedding, doc_hash, created_ns
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(span_id) DO NOTHING
                        """,
                        (
                            span_id_hex,
                            trace_id_hex,
                            encoder,
                            emb_blob,
                            doc_hash,
                            span.end_time,
                        ),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    with contextlib.suppress(sqlite3.OperationalError):
                        conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
        except Exception as e:
            _LOG.error(
                "bijotel fingerprint write failed (span not fingerprinted, host unaffected): "
                "%s: %s",
                type(e).__name__,
                e,
            )

    def shutdown(self) -> None:
        """SpanProcessor interface: no-op."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        """SpanProcessor interface: no-op (writes are sync in on_end)."""
        return True


# Also expose canonical_body extraction so callers can fingerprint manually.
def fingerprint_canonical_body(
    canonical_body: bytes,
    fingerprinter: DeterministicFingerprinter | SemanticFingerprinter | None = None,
) -> tuple[np.ndarray, str] | None:
    """Fingerprint the text content of a chain entry's canonical_body BLOB.

    Returns (embedding, doc_hash) or None if no text found. Useful for
    backfilling fingerprints over an existing chain.db without re-running
    spans through SpanProcessors.
    """
    fp = fingerprinter or DeterministicFingerprinter()
    try:
        d = json.loads(canonical_body)
    except Exception:
        return None
    msgs = d.get("attributes", {}).get("gen_ai.input.messages")
    if not msgs:
        return None
    parts: list[str] = []
    for m in msgs if isinstance(msgs, list) else []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for p in content:
                if isinstance(p, dict):
                    t = p.get("text") or p.get("content") or ""
                    if isinstance(t, str):
                        parts.append(t)
        ps = m.get("parts")
        if isinstance(ps, list):
            for p in ps:
                if isinstance(p, dict):
                    t = p.get("content") or p.get("text") or ""
                    if isinstance(t, str):
                        parts.append(t)
    text = " ".join(p for p in parts if p).strip()
    if not text:
        return None
    return fp.fingerprint(text), fp.document_hash(text)


def similarity_search(
    db_path: str | Path,
    query_text: str,
    *,
    threshold: float = 0.85,
    fingerprinter: DeterministicFingerprinter | SemanticFingerprinter | None = None,
    limit: int = 10,
) -> list[dict]:
    """Find fingerprinted spans similar to ``query_text``.

    Naive linear scan (acceptable up to ~100K rows; for larger scales,
    replace with an ANN index — out of scope for v0.7.0).

    Args:
        db_path: SQLite db with ``fingerprints`` table (same db as chain.db
            recommended).
        query_text: Plain text to embed and compare.
        threshold: Minimum cosine similarity in [-1, 1]. Default 0.85 (high
            confidence near-duplicate; tune per use case).
        fingerprinter: Must match the encoder used at ingest time
            (``protocol_id`` strings must equal). Defaults to
            :class:`DeterministicFingerprinter`.
        limit: Max results to return.

    Returns:
        List of ``{span_id, trace_id, similarity, doc_hash, created_ns}``
        sorted by descending similarity. Empty if no matches above threshold.
    """
    fp = fingerprinter or DeterministicFingerprinter()
    query_emb = fp.fingerprint(query_text)

    out: list[dict] = []
    with sqlite3.connect(db_path) as conn:
        for span_id, trace_id, encoder, emb_blob, doc_hash, created_ns in conn.execute(
            "SELECT span_id, trace_id, encoder, embedding, doc_hash, created_ns "
            "FROM fingerprints"
        ):
            if encoder != fp.protocol_id:
                # Encoder mismatch: embeddings live in different vector spaces;
                # cosine similarity across them is meaningless.
                continue
            emb = np.frombuffer(emb_blob, dtype=np.float32)
            sim = fp.similarity(query_emb, emb)
            if sim >= threshold:
                out.append(
                    {
                        "span_id": span_id,
                        "trace_id": trace_id,
                        "similarity": sim,
                        "doc_hash": doc_hash,
                        "created_ns": created_ns,
                    }
                )
    out.sort(key=lambda x: -x["similarity"])
    return out[:limit]
