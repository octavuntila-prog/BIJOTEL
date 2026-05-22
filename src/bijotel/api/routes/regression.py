"""``/regression/*`` routes — drift detection over the chain.

Endpoints:
  * ``GET  /regression/latest``    last persisted run
  * ``GET  /regression/history``   timeline of past runs
  * ``POST /regression/run``       execute a fresh run (optionally persist)

Persistence
===========

Runs are written to a small SQLite table ``regression_runs`` *inside* the
existing chain.db. We don't take a second DB file because:

* The chain DB is already the canonical artifact deployed alongside the
  application — putting regression history beside it means a single
  backup or scrub covers everything.
* It's a tiny table (one row per run, ~200 bytes); no overhead.
* Reads happen on a fresh connection; no lock contention with the
  HMAC-chain writer (which is also on this DB) because we use
  ``BEGIN IMMEDIATE`` + busy_timeout (same pattern as the chain).

Schema::

    CREATE TABLE regression_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ns INTEGER NOT NULL,
        window INTEGER NOT NULL,
        z_threshold REAL NOT NULL,
        filter_model TEXT,
        total_anomalies INTEGER NOT NULL,
        status TEXT NOT NULL,
        result_json TEXT NOT NULL  -- full RegressionRunResponse, serialized
    )

Read paths reconstruct the full Pydantic shape from ``result_json``;
``status`` / ``total_anomalies`` / ``created_ns`` are also stored as
columns so history listings don't have to parse JSON for every row.

Provenance: BIJOTEL-original, v1.1.0.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from bijotel.api.models import (
    AnomalyDetail,
    RegressionDimensionResult,
    RegressionHistoryEntry,
    RegressionHistoryResponse,
    RegressionRunRequest,
    RegressionRunResponse,
)
from bijotel.regression import RegressionDetector

router = APIRouter(prefix="/regression", tags=["regression"])


# ───────────────────────── helpers ─────────────────────────


def _db_path(request: Request) -> Path:
    return Path(request.app.state.db_path)


def _ensure_db(path: Path) -> None:
    if not path.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"Chain DB not found at {path}. "
            "Has any HMAC-chain processor written to it yet?",
        )


def _ensure_runs_table(db_path: Path) -> None:
    """Create the regression_runs table if missing.

    Done lazily on first /regression/* hit (rather than at create_app
    time) so the API works against read-only chain DBs too — if the DB
    can't be opened for writing, the user simply gets a clearer error
    when they POST /regression/run.
    """
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regression_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ns INTEGER NOT NULL,
                window INTEGER NOT NULL,
                z_threshold REAL NOT NULL,
                filter_model TEXT,
                total_anomalies INTEGER NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        conn.execute("COMMIT")


def _ns_to_iso(ns: int | None) -> str:
    if ns is None:
        return ""
    return (
        datetime.fromtimestamp(ns / 1e9, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _anomaly_to_detail(a: object, dim: str) -> AnomalyDetail:
    """Project a regression Anomaly (frozen dataclass) into the API model."""
    return AnomalyDetail(
        dimension=dim,
        seq=a.seq,  # type: ignore[attr-defined]
        timestamp=a.timestamp,  # type: ignore[attr-defined]
        value=float(a.value),  # type: ignore[attr-defined]
        baseline_mean=float(a.baseline_mean),  # type: ignore[attr-defined]
        z_score=(float(a.z_score) if a.z_score is not None else None),  # type: ignore[attr-defined]
        iqr_distance=(
            float(a.iqr_distance) if a.iqr_distance is not None else None  # type: ignore[attr-defined]
        ),
        method_triggered=a.method_triggered,  # type: ignore[attr-defined]
        severity=a.severity,  # type: ignore[attr-defined]
    )


def _per_dim_stats(
    db_path: Path, dimension: str, window: int
) -> RegressionDimensionResult:
    """Compute baseline mean/std for the most recent `window` rows of a dimension.

    Lightweight summary — only used to populate the response's
    ``dimensions`` block. The detector itself computes baseline internally.
    """
    from bijotel.regression.baseline import _extract_dimension_value

    values: list[float] = []
    sql = "SELECT canonical_body FROM chain ORDER BY seq DESC LIMIT ?"
    with sqlite3.connect(db_path) as conn:
        for (body_blob,) in conn.execute(sql, (window,)):
            try:
                body = json.loads(
                    body_blob.decode("utf-8")
                    if isinstance(body_blob, bytes)
                    else body_blob
                )
            except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
                continue
            v = _extract_dimension_value(body, dimension)
            if v is not None:
                values.append(float(v))

    samples = len(values)
    if samples < 2:
        return RegressionDimensionResult(
            baseline_mean=None,
            baseline_std=None,
            samples=samples,
            anomalies=0,
            status="insufficient_data",
        )

    return RegressionDimensionResult(
        baseline_mean=round(statistics.fmean(values), 4),
        baseline_std=round(statistics.stdev(values), 4) if samples >= 2 else None,
        samples=samples,
        anomalies=0,  # populated by caller after detect()
        status="clean",  # populated by caller
    )


def _run_detection(
    db_path: Path,
    *,
    window: int,
    z_threshold: float,
    filter_model: str | None,
) -> RegressionRunResponse:
    """Execute one regression scan across all dimensions; build response."""
    detector = RegressionDetector(
        db_path=db_path,
        baseline_window=window,
        z_threshold=z_threshold,
    )

    dim_results: dict[str, RegressionDimensionResult] = {}
    flat_details: list[AnomalyDetail] = []

    per_dim = detector.detect_all_dimensions(filter_model=filter_model)

    for dim, anomalies in per_dim.items():
        stats = _per_dim_stats(db_path, dim, window)
        stats.anomalies = len(anomalies)
        if stats.status != "insufficient_data":
            stats.status = "anomaly" if anomalies else "clean"
        dim_results[dim] = stats
        for a in anomalies:
            flat_details.append(_anomaly_to_detail(a, dim))

    total = sum(s.anomalies for s in dim_results.values())
    insufficient = all(
        s.status == "insufficient_data" for s in dim_results.values()
    )
    overall = (
        "insufficient_data" if insufficient else ("anomaly" if total else "clean")
    )

    return RegressionRunResponse(
        run_id=None,
        timestamp=_ns_to_iso(time.time_ns()),
        window=window,
        z_threshold=z_threshold,
        dimensions=dim_results,
        details=flat_details,
        total_anomalies=total,
        status=overall,
    )


def _persist_run(db_path: Path, response: RegressionRunResponse) -> int:
    """Insert a run; return its row id."""
    _ensure_runs_table(db_path)
    with sqlite3.connect(db_path, isolation_level=None) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            INSERT INTO regression_runs
              (created_ns, window, z_threshold, filter_model,
               total_anomalies, status, result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time_ns(),
                response.window,
                response.z_threshold,
                None,
                response.total_anomalies,
                response.status,
                response.model_dump_json(),
            ),
        )
        run_id = cur.lastrowid
        conn.execute("COMMIT")
    return int(run_id) if run_id else 0


# ───────────────────────── POST /regression/run ─────────────────────────


@router.post(
    "/run",
    response_model=RegressionRunResponse,
    summary="Execute a fresh regression analysis",
)
def regression_run(
    request: Request, payload: RegressionRunRequest | None = None
) -> RegressionRunResponse:
    """Run drift detection now and (by default) persist the result.

    Pass ``persist=false`` for a dry-run that doesn't enter the history.
    """
    path = _db_path(request)
    _ensure_db(path)
    p = payload or RegressionRunRequest()

    result = _run_detection(
        path,
        window=p.window,
        z_threshold=p.z_threshold,
        filter_model=p.filter_model,
    )
    if p.persist:
        result.run_id = _persist_run(path, result)
    return result


# ───────────────────────── GET /regression/latest ─────────────────────────


@router.get(
    "/latest",
    response_model=RegressionRunResponse,
    summary="Return the most recently persisted regression run",
)
def regression_latest(request: Request) -> RegressionRunResponse:
    """Return the latest stored run, or 404 if none exist yet."""
    path = _db_path(request)
    _ensure_db(path)
    _ensure_runs_table(path)

    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT id, result_json FROM regression_runs "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if row is None:
        raise HTTPException(
            404,
            "No regression runs persisted yet. POST /regression/run first.",
        )
    run_id, blob = row
    data = json.loads(blob)
    data["run_id"] = run_id
    return RegressionRunResponse.model_validate(data)


# ───────────────────────── GET /regression/history ─────────────────────────


@router.get(
    "/history",
    response_model=RegressionHistoryResponse,
    summary="Paginated timeline of past regression runs",
)
def regression_history(
    request: Request,
    limit: int = Query(24, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> RegressionHistoryResponse:
    """Return a lightweight list of past runs (no full payload, just summary)."""
    path = _db_path(request)
    _ensure_db(path)
    _ensure_runs_table(path)

    with sqlite3.connect(path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM regression_runs").fetchone()[0]
        rows = conn.execute(
            """
            SELECT id, created_ns, window, total_anomalies, status
            FROM regression_runs
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    return RegressionHistoryResponse(
        runs=[
            RegressionHistoryEntry(
                run_id=r[0],
                timestamp=_ns_to_iso(r[1]),
                window=r[2],
                total_anomalies=r[3],
                status=r[4],
            )
            for r in rows
        ],
        total_runs=int(total),
    )


__all__ = ["router"]
