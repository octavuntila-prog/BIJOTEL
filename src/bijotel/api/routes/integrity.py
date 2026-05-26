"""``/integrity`` — chain-integrity anomaly detection (v2.8.0).

One endpoint: ``GET /integrity?window=N``.

Runs :func:`bijotel.integrity.analyze_chain_integrity` against the host
app's chain DB and returns the report as JSON. Read-only, fast (~50 ms
for a 100-row window on a 6000-entry chain).

Status codes:
    200 — report returned (`clean` field carries the verdict).
    503 — chain DB missing on disk.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from bijotel.api.models import IntegrityReportResponse
from bijotel.integrity import analyze_chain_integrity

router = APIRouter(prefix="/integrity", tags=["integrity"])

_LOG = logging.getLogger("bijotel.api.integrity")


def _db_path(request: Request) -> Path:
    return Path(request.app.state.db_path)


@router.get("", response_model=IntegrityReportResponse)
def integrity_report(
    request: Request,
    window: int = Query(
        100,
        ge=2,
        le=10000,
        description="How many recent chain entries to analyze.",
    ),
) -> IntegrityReportResponse:
    """Analyze chain integrity over the last ``window`` entries."""
    db = _db_path(request)
    if not db.exists():
        raise HTTPException(
            status_code=503,
            detail=f"chain DB not found at {db}",
        )

    report = analyze_chain_integrity(db, window=window)
    return IntegrityReportResponse(**report.to_dict())
