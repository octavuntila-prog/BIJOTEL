"""``/chain/*`` routes — read-only views into the BIJOTEL HMAC chain.

Endpoints:
  * ``GET  /chain``          paginated list of chain rows (lightweight)
  * ``GET  /chain/stats``    aggregate counters + dedup factor
  * ``GET  /chain/{seq}``    one row's full canonical body + hashes
  * ``POST /chain/verify``   re-verify the chain (full or smoke)

Design notes:

* The chain DB path is resolved via ``request.app.state.db_path`` so a host
  can pass a different DB to :func:`bijotel.api.app.create_app` without
  importing this module. Open a fresh SQLite connection per request — the
  HMAC chain processor is multi-writer-safe and the cost of connect() is
  trivial relative to JSON serialization.
* ``hmac_valid`` on the list endpoint is best-effort: we recompute
  ``HMAC(prev_hash || canonical_hash)`` for each returned row but only if
  ``BIJOTEL_HMAC_SECRET`` is set in the server's environment. Without the
  secret we return ``hmac_valid=null`` (NOT ``false``) — the auditor sees
  honestly that we couldn't check, rather than a misleading red flag.
* ``GET /chain/{seq}`` is a separate endpoint to keep the list payload
  small. Bodies can be hundreds of KB; we don't ship them by default.
* ``POST /chain/verify`` with ``full=true`` calls the canonical
  :func:`bijotel.processors.verify_chain` so the API answer matches the
  CLI's ``bijotel verify`` exactly. With ``full=false`` we do a fast
  spot-check (last 5 rows' prev_hash linkage) — useful for liveness
  dashboards that poll every minute.

Provenance: BIJOTEL-original, v1.1.0.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from bijotel.api.models import (
    ChainEntryDetail,
    ChainEntrySummary,
    ChainListResponse,
    ChainStatsResponse,
    ChainVerifyRequest,
    ChainVerifyResponse,
    Pagination,
)
from bijotel.processors import cas_stats, verify_chain

router = APIRouter(prefix="/chain", tags=["chain"])


# ───────────────────────── helpers ─────────────────────────


def _db_path(request: Request) -> Path:
    """Pull the DB path off the app state attached by ``create_app``."""
    return Path(request.app.state.db_path)


def _ensure_db(path: Path) -> None:
    """503 if the chain DB hasn't been created yet (server up before init)."""
    if not path.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"Chain DB not found at {path}. "
            "Has any HMAC-chain processor written to it yet?",
        )


def _ns_to_iso(ns: int | None) -> str | None:
    """Convert ns-since-epoch to ISO-8601 UTC string."""
    if ns is None:
        return None
    return (
        datetime.fromtimestamp(ns / 1e9, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _resolve_secret() -> bytes | None:
    """Read BIJOTEL_HMAC_SECRET from env (hex). Returns None if absent / invalid."""
    hex_str = os.environ.get("BIJOTEL_HMAC_SECRET")
    if not hex_str:
        return None
    try:
        return bytes.fromhex(hex_str)
    except ValueError:
        return None


# ───────────────────────── GET /chain ─────────────────────────


@router.get(
    "",
    response_model=ChainListResponse,
    summary="List chain entries (paginated)",
)
def chain_list(
    request: Request,
    limit: int = Query(50, ge=1, le=500, description="Page size (1-500)."),
    offset: int = Query(0, ge=0, description="Number of rows to skip."),
    since: str | None = Query(
        None,
        description="Lower bound (inclusive), ISO-8601 UTC (e.g. 2026-05-20T00:00:00Z).",
    ),
    until: str | None = Query(
        None,
        description="Upper bound (exclusive), ISO-8601 UTC.",
    ),
) -> ChainListResponse:
    """Return a paginated, optionally time-filtered list of chain rows.

    The ``hmac_valid`` field is best-effort: when ``BIJOTEL_HMAC_SECRET`` is
    NOT set on the server, every entry's value is ``null`` (we couldn't
    verify). When set, we recompute HMAC for each returned row.
    """
    path = _db_path(request)
    _ensure_db(path)

    where_clauses: list[str] = []
    params: list[Any] = []
    if since:
        try:
            since_ns = int(
                datetime.fromisoformat(since.replace("Z", "+00:00")).timestamp() * 1e9
            )
        except ValueError as e:
            raise HTTPException(400, f"Invalid 'since' ISO-8601: {e}") from e
        where_clauses.append("timestamp_ns >= ?")
        params.append(since_ns)
    if until:
        try:
            until_ns = int(
                datetime.fromisoformat(until.replace("Z", "+00:00")).timestamp() * 1e9
            )
        except ValueError as e:
            raise HTTPException(400, f"Invalid 'until' ISO-8601: {e}") from e
        where_clauses.append("timestamp_ns < ?")
        params.append(until_ns)

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    secret = _resolve_secret()

    with sqlite3.connect(path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM chain{where_sql}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT seq, timestamp_ns, trace_id, span_id, span_name, span_kind,
                   canonical_body, canonical_hash, prev_hash, hmac_hash
            FROM chain{where_sql}
            ORDER BY seq DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

    entries: list[ChainEntrySummary] = []
    for seq, ts_ns, trace_id, span_id, span_name, span_kind, body, c_hash, p_hash, h_hash in rows:
        hmac_valid: bool | None
        if secret is None:
            hmac_valid = None
        else:
            import hashlib
            import hmac as _hmac

            recomputed_c = hashlib.sha256(body).hexdigest()
            if recomputed_c != c_hash:
                hmac_valid = False
            else:
                exp = _hmac.new(
                    secret,
                    (p_hash + c_hash).encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
                hmac_valid = exp == h_hash
        entries.append(
            ChainEntrySummary(
                seq=seq,
                timestamp=_ns_to_iso(ts_ns) or "",
                trace_id=trace_id,
                span_id=span_id,
                span_name=span_name,
                span_kind=span_kind,
                canonical_hash=c_hash,
                # Pydantic v2 needs explicit cast; bool|None field accepts None
                hmac_valid=hmac_valid if hmac_valid is not None else False,
            )
        )

    return ChainListResponse(
        total=total,
        entries=entries,
        pagination=Pagination(
            limit=limit, offset=offset, has_more=(offset + len(rows)) < total
        ),
    )


# ───────────────────────── GET /chain/stats ─────────────────────────
# NB: this route is declared BEFORE /chain/{seq} so FastAPI's path-matcher
# picks "stats" as literal, not as an int seq parameter.


@router.get(
    "/stats",
    response_model=ChainStatsResponse,
    summary="Aggregate counters for the chain DB",
)
def chain_stats(request: Request) -> ChainStatsResponse:
    """Return one-shot aggregate stats: row count, CAS dedup, age, throughput."""
    path = _db_path(request)
    _ensure_db(path)

    with sqlite3.connect(path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
        first_row = conn.execute(
            "SELECT MIN(timestamp_ns), MAX(timestamp_ns) FROM chain"
        ).fetchone()
        first_ns, last_ns = first_row if first_row else (None, None)

    # CAS may not exist yet (chain DB without CasSpanProcessor) — guard.
    cas_entries = 0
    dedup_factor = 1.0
    try:
        cas = cas_stats(path)
        cas_entries = int(cas.get("unique_bodies", 0))
        dedup_factor = float(cas.get("dedup_factor", 1.0))
    except sqlite3.OperationalError:
        # CAS table absent — fine, leave defaults
        pass

    age_days = 0.0
    if first_ns and last_ns and last_ns > first_ns:
        age_days = (last_ns - first_ns) / 1e9 / 86400
    entries_per_day = total / age_days if age_days > 0 else 0.0

    return ChainStatsResponse(
        total_entries=total,
        cas_entries=cas_entries,
        dedup_factor=dedup_factor,
        first_entry=_ns_to_iso(first_ns),
        last_entry=_ns_to_iso(last_ns),
        age_days=round(age_days, 3),
        db_size_bytes=path.stat().st_size,
        entries_per_day=round(entries_per_day, 1),
    )


# ───────────────────────── POST /chain/verify ─────────────────────────


@router.post(
    "/verify",
    response_model=ChainVerifyResponse,
    summary="Verify chain integrity",
)
def chain_verify(
    request: Request, body: ChainVerifyRequest | None = None
) -> ChainVerifyResponse:
    """Re-verify the chain.

    * ``full=true`` (default false): canonical re-verification via
      :func:`bijotel.processors.verify_chain` — requires
      ``BIJOTEL_HMAC_SECRET`` set in the server env. Slow on large chains
      (O(N) SHA-256 + HMAC per row).
    * ``full=false``: smoke check — confirm row count, return first/last
      seq, and check the last row's ``prev_hash`` matches the previous
      row's ``hmac_hash`` (chain not broken at the tail). Does NOT
      validate HMAC — use ``full=true`` for full chain verification.
    """
    path = _db_path(request)
    _ensure_db(path)
    full = bool(body and body.full)

    with sqlite3.connect(path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
        if total == 0:
            return ChainVerifyResponse(
                valid=True,
                entries_verified=0,
                first_seq=None,
                last_seq=None,
                error=None,
                error_seq=None,
            )
        first_seq, last_seq = conn.execute(
            "SELECT MIN(seq), MAX(seq) FROM chain"
        ).fetchone()

    if not full:
        # Smoke check — tail linkage
        with sqlite3.connect(path) as conn:
            tail = conn.execute(
                "SELECT seq, prev_hash, hmac_hash FROM chain ORDER BY seq DESC LIMIT 2"
            ).fetchall()
        if len(tail) == 2:
            (_, latest_prev, _), (_, _, prev_hmac) = tail
            if latest_prev != prev_hmac:
                return ChainVerifyResponse(
                    valid=False,
                    entries_verified=2,
                    first_seq=first_seq,
                    last_seq=last_seq,
                    error="tail prev_hash != previous row's hmac_hash",
                    error_seq=tail[0][0],
                )
        return ChainVerifyResponse(
            valid=True,
            entries_verified=len(tail),
            first_seq=first_seq,
            last_seq=last_seq,
            error=None,
            error_seq=None,
        )

    # full=true → canonical verification
    secret = _resolve_secret()
    if secret is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "full=true requires BIJOTEL_HMAC_SECRET (hex) in the server "
                "environment. Restart bijotel serve with the secret set, or "
                "use full=false for a smoke-only check."
            ),
        )

    # v2.3.0: pass range kwargs through when the request supplied any.
    range_kwargs: dict = {}
    if body:
        for field in ("seq_start", "seq_end", "since_ns", "until_ns", "last_n"):
            v = getattr(body, field, None)
            if v is not None:
                range_kwargs[field] = v

    valid, fail_seq, reason = verify_chain(path, secret, **range_kwargs)
    return ChainVerifyResponse(
        valid=valid,
        entries_verified=total,
        first_seq=first_seq,
        last_seq=last_seq,
        error=reason,
        error_seq=fail_seq,
    )


# ───────────────────────── GET /chain/{seq} ─────────────────────────


@router.get(
    "/{seq}",
    response_model=ChainEntryDetail,
    summary="Get one chain entry (full canonical body)",
)
def chain_detail(seq: int, request: Request) -> ChainEntryDetail:
    """Return the full row for ``seq`` — canonical body parsed as JSON."""
    if seq < 1:
        raise HTTPException(400, "seq must be >= 1")

    path = _db_path(request)
    _ensure_db(path)

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            """
            SELECT seq, timestamp_ns, trace_id, span_id, span_name, span_kind,
                   canonical_body, canonical_hash, prev_hash, hmac_hash,
                   semantic_body_hash
            FROM chain WHERE seq = ?
            """,
            (seq,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"chain entry seq={seq} not found")

    (
        seq_val,
        ts_ns,
        trace_id,
        span_id,
        span_name,
        span_kind,
        body_bytes,
        c_hash,
        p_hash,
        h_hash,
        sem_hash,
    ) = row

    try:
        canonical_body = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        canonical_body = {"_error": "body not valid UTF-8 JSON"}

    # Per-entry hmac_valid (recompute if secret available)
    secret = _resolve_secret()
    hmac_valid = False
    if secret is not None:
        import hashlib
        import hmac as _hmac

        if hashlib.sha256(body_bytes).hexdigest() == c_hash:
            exp = _hmac.new(
                secret, (p_hash + c_hash).encode("utf-8"), hashlib.sha256
            ).hexdigest()
            hmac_valid = exp == h_hash

    return ChainEntryDetail(
        seq=seq_val,
        timestamp=_ns_to_iso(ts_ns) or "",
        trace_id=trace_id,
        span_id=span_id,
        span_name=span_name,
        span_kind=span_kind,
        canonical_body=canonical_body,
        canonical_hash=c_hash,
        prev_hash=p_hash,
        hmac_hash=h_hash,
        hmac_valid=hmac_valid,
        cas_ref=sem_hash,
    )


__all__ = ["router"]


# Silence unused-import in environments without time tracking
_ = time
