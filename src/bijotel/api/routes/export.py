"""``/export`` + ``/export/verify`` — portable signed chain export.

Wraps :func:`bijotel.processors.export.export_chain` and
:func:`bijotel.processors.export.verify_export` so external auditors can
pull a tamper-evident JSON snapshot over HTTP and (later, possibly on a
different host) verify its integrity using only the shared HMAC secret.

Why server-side instead of just running the CLI?

* The dashboard (v1.2.0) needs a "Download audit trail" button.
* Auditors can verify without having SSH access to the server.
* Per Combo D, the act of exporting + delivering the trail is itself
  worth recording — a future iteration could log this on the chain.

Schema: ``bijotel-chain-v1`` (see :mod:`bijotel.processors.export`).

Provenance: BIJOTEL-original, v1.1.0. Underlying export/verify logic
adapted from substrate-guard ``chain.py`` (separate project, read-only).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from bijotel.api.models import ExportVerifyResponse
from bijotel.processors import export_chain, verify_export

router = APIRouter(prefix="/export", tags=["export"])


def _db_path(request: Request) -> Path:
    return Path(request.app.state.db_path)


def _resolve_secret() -> bytes:
    """Read BIJOTEL_HMAC_SECRET (hex). 400 if absent — export needs it.

    Unlike ``/chain`` where the secret is optional (we just report hmac_valid
    honestly), ``/export`` *cannot* produce a signed file without the secret.
    The auditor would have nothing to verify against.
    """
    hex_str = os.environ.get("BIJOTEL_HMAC_SECRET")
    if not hex_str:
        raise HTTPException(
            status_code=400,
            detail=(
                "POST /export requires BIJOTEL_HMAC_SECRET (hex) in the server "
                "environment — the export file is signed with it. Set the env "
                "var on the bijotel-serve process and try again."
            ),
        )
    try:
        return bytes.fromhex(hex_str)
    except ValueError as e:
        raise HTTPException(
            status_code=500,
            detail=f"BIJOTEL_HMAC_SECRET is not valid hex: {e}",
        ) from e


# ───────────────────────── POST /export ─────────────────────────


@router.post(
    "",
    summary="Export the chain as a signed JSON file (download)",
    response_class=FileResponse,
)
def export_post(request: Request) -> FileResponse:
    """Generate a fresh signed JSON snapshot of the chain and stream it back.

    Returns a ``FileResponse`` with ``Content-Disposition: attachment`` and
    a timestamped filename. The temp file is created in the OS temp dir;
    FastAPI's BackgroundTask machinery wipes it when the response is done.

    Validation: requires ``BIJOTEL_HMAC_SECRET`` (hex) in the server env.
    The exported file is signed with it; the auditor will verify with the
    same key (or its hex form) via ``POST /export/verify``.
    """
    db = _db_path(request)
    if not db.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"Chain DB not found at {db}.",
        )

    secret = _resolve_secret()

    # Materialize to a temp file so FileResponse can stream it
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="bijotel-export-")
    os.close(fd)
    out_path = Path(tmp_path)

    try:
        export_chain(db, out_path, secret)
    except Exception as e:
        out_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail=f"export failed: {type(e).__name__}: {e}"
        ) from e

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    filename = f"bijotel-export-{timestamp}.json"

    # NOTE: FileResponse copies headers; we set a download disposition.
    # The temp file is deleted via the response's background task — FastAPI
    # honors the ``background`` argument on FileResponse for cleanup. We use
    # a lightweight inline lambda task to unlink after the body is sent.
    from starlette.background import BackgroundTask

    return FileResponse(
        path=str(out_path),
        media_type="application/json",
        filename=filename,
        background=BackgroundTask(lambda: out_path.unlink(missing_ok=True)),
    )


# ───────────────────────── POST /export/verify ─────────────────────────


@router.post(
    "/verify",
    response_model=ExportVerifyResponse,
    summary="Verify an exported chain JSON file",
)
async def export_verify(
    file: UploadFile = File(  # noqa: B008 — FastAPI idiomatic parameter default
        ...,
        description="Signed JSON file produced by POST /export "
        "(or CLI 'bijotel export'). Must match bijotel-chain-v1 schema.",
    ),
) -> ExportVerifyResponse:
    """Upload an exported file; return validity + first-failure reason.

    Uses :func:`bijotel.processors.export.verify_export` directly. The
    server's ``BIJOTEL_HMAC_SECRET`` must equal the one used at export
    time — otherwise the per-entry HMAC recomputation fails at seq=1 with
    "hmac_hash mismatch".

    Limits: the upload is materialized to a temp file (no in-memory parse
    of a large JSON blob). FastAPI's ``UploadFile`` already spools to disk
    above 1MB.
    """
    secret = _resolve_secret()

    # Spool the upload to a temp file (avoid huge in-memory dict)
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="bijotel-verify-")
    os.close(fd)
    out_path = Path(tmp_path)
    try:
        body = await file.read()
        out_path.write_bytes(body)

        # Best-effort parse for the metadata fields we report on either branch
        meta_entries = None
        meta_head = None
        meta_format = None
        try:
            with out_path.open("rb") as fh:
                meta = json.load(fh)
            meta_entries = int(meta.get("entries_count", 0)) or None
            meta_head = meta.get("head_hash")
            meta_format = meta.get("format")
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

        valid, reason = verify_export(out_path, secret)
        return ExportVerifyResponse(
            valid=valid,
            reason=reason,
            entries_count=meta_entries,
            head_hash=meta_head,
            format=meta_format,
        )
    finally:
        out_path.unlink(missing_ok=True)


__all__ = ["router"]
