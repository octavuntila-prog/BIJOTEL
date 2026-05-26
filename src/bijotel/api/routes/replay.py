"""``/replay/*`` — deterministic-seed replay verification (v2.7.0).

One endpoint: ``POST /replay/verify``.

Reads the chain entry by seq, computes SHA-256 of the caller-supplied
replayed output, and compares against the sealed
``bijotel.replay.output_hash``. Pure hash comparison — never re-executes
an LLM call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from bijotel.api.models import ReplayVerifyRequest, ReplayVerifyResponse
from bijotel.replay import verify_replay

router = APIRouter(prefix="/replay", tags=["replay"])

_LOG = logging.getLogger("bijotel.api.replay")


def _db_path(request: Request) -> Path:
    return Path(request.app.state.db_path)


@router.post("/verify", response_model=ReplayVerifyResponse)
def replay_verify(
    payload: ReplayVerifyRequest,
    request: Request,
) -> ReplayVerifyResponse:
    """Verify a replayed output against a sealed chain entry.

    Returns:
        200 + ``ReplayVerifyResponse`` for both match and mismatch
        outcomes. (We return 200 even on mismatch — the comparison
        succeeded; the *answer* is "no match". HTTP status 4xx/5xx is
        reserved for "the comparison itself couldn't run".)

    Raises:
        404 if ``seq`` is not in the chain.
        503 if the chain DB is missing on disk.
    """
    db = _db_path(request)
    if not db.exists():
        raise HTTPException(
            status_code=503,
            detail=f"chain DB not found at {db}",
        )

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT canonical_body FROM chain WHERE seq = ?",
            (int(payload.seq),),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"seq {payload.seq} not found in chain",
            )

    body_raw = row[0]
    body_dict = json.loads(
        body_raw.decode("utf-8") if isinstance(body_raw, bytes) else body_raw
    )
    result = verify_replay(body_dict, payload.replayed_output)

    return ReplayVerifyResponse(**result.to_dict())
