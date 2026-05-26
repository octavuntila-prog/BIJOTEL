"""``/keygen``, ``/archive``, ``/verify-continuity`` (v2.3.0).

Surfaces the v2.1.0 + v2.2.0 features that previously shipped CLI-only
into the REST API so ``bijotel serve`` exposes the full feature set.

Provenance: BIJOTEL-original, v2.3.0 — closes the internal-audit
finding "v2.1/v2.2 features are CLI-only" (2026-05-26).
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from bijotel.api.models import (
    ArchiveRequest,
    ArchiveResponse,
    KeygenRequest,
    KeygenResponse,
    VerifyContinuityRequest,
    VerifyContinuityResponse,
)
from bijotel.crypto.ed25519 import (
    generate_keypair,
    public_key_fingerprint,
)
from bijotel.processors.archive import archive_chain, verify_continuity

router = APIRouter(tags=["archive"])


# ───────────────────────── /keygen ─────────────────────────


@router.post(
    "/keygen",
    response_model=KeygenResponse,
    summary="Generate an Ed25519 keypair for signing exports (v2.1.0+).",
)
def keygen(payload: KeygenRequest) -> KeygenResponse:
    """Write a fresh Ed25519 keypair to disk and return the public key.

    The private key stays on the server filesystem at the returned
    ``private_key_path``. The public key is returned inline so the
    caller can distribute it to auditors. Auditors then verify
    signed exports with the public key alone, never holding the HMAC
    secret.

    The handler refuses to overwrite an existing private key unless
    ``force=true`` — rotating a signing key invalidates every
    historically signed export under the old key, so it must be an
    explicit decision.
    """
    out_dir = Path(payload.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    priv_path = out_dir / "bijotel_private.pem"
    pub_path = out_dir / "bijotel_public.pem"

    if priv_path.exists() and not payload.force:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{priv_path} already exists. Pass force=true to overwrite "
                "(this rotates the signing key — old signed exports will "
                "no longer carry a matching key)."
            ),
        )

    private_pem, public_pem = generate_keypair()
    priv_path.write_bytes(private_pem)
    pub_path.write_bytes(public_pem)
    # Lock down on POSIX; no-op on Windows / restricted FS.
    with contextlib.suppress(OSError):
        os.chmod(priv_path, 0o600)

    return KeygenResponse(
        private_key_path=str(priv_path.resolve()),
        public_key_pem=public_pem.decode("ascii"),
        fingerprint=public_key_fingerprint(public_pem),
    )


# ───────────────────────── /archive ─────────────────────────


def _resolve_hmac_secret() -> bytes:
    """Read BIJOTEL_HMAC_SECRET from env; 400 if absent."""
    hex_str = os.environ.get("BIJOTEL_HMAC_SECRET")
    if not hex_str:
        raise HTTPException(
            status_code=400,
            detail=(
                "POST /archive requires BIJOTEL_HMAC_SECRET (hex) in the "
                "server environment — the archive is verified after "
                "creation and the source DB is re-verified after delete."
            ),
        )
    try:
        return bytes.fromhex(hex_str)
    except ValueError as e:
        raise HTTPException(
            status_code=500,
            detail=f"BIJOTEL_HMAC_SECRET is not valid hex: {e}",
        ) from e


def _resolve_db_path(request: Request) -> Path:
    db = getattr(request.app.state, "db_path", None)
    if not db:
        raise HTTPException(status_code=503, detail="No db_path configured.")
    p = Path(db)
    if not p.is_file():
        raise HTTPException(status_code=503, detail=f"Chain DB not found: {db}")
    return p


@router.post(
    "/archive",
    response_model=ArchiveResponse,
    summary="Peel oldest entries off into a separate archive DB (v2.2.0+).",
)
def archive(payload: ArchiveRequest, request: Request) -> ArchiveResponse:
    """Build a fresh archive DB, verify it, then delete the archived rows
    from source in a single transaction.

    The endpoint enforces the same safety contract as the CLI:

    * The archive is built and verified BEFORE any DELETE on source.
    * If verification fails the source is left untouched.
    * ``dry_run=true`` reports the plan without writing the archive
      or deleting anything.

    Exactly one of ``before_seq`` / ``before_iso`` must be supplied.
    """
    if (payload.before_seq is None) == (payload.before_iso is None):
        raise HTTPException(
            status_code=400,
            detail="Pass exactly one of before_seq or before_iso.",
        )

    db_path = _resolve_db_path(request)
    secret = _resolve_hmac_secret()

    before_ns: int | None = None
    if payload.before_iso is not None:
        try:
            dt = datetime.strptime(payload.before_iso, "%Y-%m-%d").replace(
                tzinfo=UTC
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=f"before_iso must be YYYY-MM-DD; got {payload.before_iso!r}",
            ) from e
        before_ns = int(dt.timestamp() * 1e9)

    archive_path = Path(payload.output_path)

    try:
        report = archive_chain(
            db_path,
            archive_path,
            secret,
            before_ns=before_ns,
            before_seq=payload.before_seq,
            sign_key_path=payload.sign_key_path,
            dry_run=payload.dry_run,
        )
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    return ArchiveResponse(**report)


# ───────────────────────── /verify-continuity ─────────────────────────


@router.post(
    "/verify-continuity",
    response_model=VerifyContinuityResponse,
    summary="Verify chain continuity across an ordered list of DBs (v2.2.0+).",
)
def verify_continuity_endpoint(
    payload: VerifyContinuityRequest,
) -> VerifyContinuityResponse:
    """Walk segments in order and check ``A.last_hmac == B.first.prev`` for
    each adjacent pair. Each segment is also reported with its own
    first/last seq and validity.

    Pass the segment DBs in chronological order (oldest archive first,
    live chain.db last). Out-of-order arguments will report `matches:
    false` on the first pair even though the data itself may be
    consistent.
    """
    result = verify_continuity(payload.db_paths)
    return VerifyContinuityResponse(**result)


__all__ = ["router"]
