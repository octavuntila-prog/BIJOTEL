"""``/cross-view`` — unified read-only view across multiple BIJOTEL chains.

Exposes the same aggregation the ``bijotel cross-view`` CLI produces
(per-ecosystem stats + optional structural integrity) over REST, so the
dashboard / API can render a GENA+ARA-style unified view. Each chain stays
sovereign: no merging, no rewriting, read-only.

NOT federation. This is the local ``bijotel.cross_view`` aggregator over
operator-supplied chain paths — the same trust model as
``/verify-continuity``'s ``db_paths``. Cross-org federation is a separate
component (``bijotel.federation`` + the federation service) and is not
touched here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from bijotel.api.models import CrossViewRequest, CrossViewResponse
from bijotel.cross_view import CrossEcosystemView

router = APIRouter(tags=["cross-view"])

_DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


@router.post(
    "/cross-view",
    response_model=CrossViewResponse,
    summary="Unified read-only view across multiple BIJOTEL chains.",
)
def cross_view_endpoint(payload: CrossViewRequest) -> CrossViewResponse:
    """Aggregate stats (and optional structural integrity) across N chains.

    Each chain is ``{name, path}``; a path ending in .db/.sqlite/.sqlite3 is
    read as SQLite, otherwise as an exported JSON. Read-only — chains are
    never merged or modified. Mirrors ``bijotel cross-view``.

    Integrity (when requested) is STRUCTURAL only over REST — no HMAC secret
    crosses the wire. Use the CLI for full HMAC verification.
    """
    view = CrossEcosystemView()
    for spec in payload.chains:
        try:
            if spec.path.endswith(_DB_SUFFIXES):
                view.add_chain(spec.name, db_path=spec.path)
            else:
                view.add_chain(spec.name, export_path=spec.path)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            # Bad spec: duplicate name, both/neither path kinds, etc.
            raise HTTPException(status_code=400, detail=str(e)) from e

    summary = view.summary()
    if payload.integrity:
        summary["integrity_report"] = view.integrity_report()
    return CrossViewResponse(**summary)


__all__ = ["router"]
