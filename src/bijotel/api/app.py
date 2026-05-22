"""Minimal FastAPI app for ``bijotel serve``.

v1.0.0 ships **only** the wiring needed to validate the API runtime:

* ``GET  /health`` — process-level liveness probe.
* ``GET  /version`` — package version + db path (forensic-trace).
* ``GET  /chain`` — placeholder (returns 501 with "Coming in v1.1.0").

The full chain explorer, policy inspector, and regression dashboard endpoints
are scheduled for v1.1.0 / v1.2.0 per the 12-day harvest plan. Shipping the
placeholders now lets users provision Docker + reverse-proxy + TLS once,
then upgrade transparently when the real endpoints land.

This module is import-safe **only** when ``fastapi`` is installed. The
package's ``bijotel.api.__init__`` uses lazy ``__getattr__`` so the
base package still imports without the ``[api]`` extra.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException
except ImportError as e:  # pragma: no cover - tested via test_serve_without_fastapi_graceful
    raise ImportError(
        "bijotel.api requires the [api] extra. Install with:\n"
        "    pip install bijotel[api]\n"
        "(this pulls in fastapi + uvicorn)"
    ) from e

from bijotel import __version__


def create_app(db_path: str | Path = "chain.db") -> FastAPI:
    """Build a fresh ``FastAPI`` instance bound to ``db_path``.

    Args:
        db_path: Path to the BIJOTEL chain.db SQLite file. Stored on the
            app instance for downstream endpoints (v1.1.0+). Not opened
            at construction time — endpoints open on demand so the
            FastAPI process can start before the DB exists.

    Returns:
        Configured :class:`FastAPI` instance with health + version routes
        and v1.1.0 placeholder routes.
    """
    db_path_str = str(db_path)

    app = FastAPI(
        title="BIJOTEL",
        description=(
            "Forensic-grade tamper-evident audit chain for LLM applications. "
            "HMAC-SHA256 chain + content-addressable storage + pre-call policy gate."
        ),
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Attach db_path for downstream endpoints (v1.1.0+ will read this)
    app.state.db_path = db_path_str

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str | bool]:
        """Liveness probe. Returns 200 if the process is up.

        Does NOT verify chain integrity — that's :func:`/verify` (v1.1.0).
        Does NOT confirm the DB file exists — that's intentional: the
        server should boot even if the chain is being initialized
        asynchronously by another process.
        """
        return {
            "status": "ok",
            "version": __version__,
            "db": db_path_str,
            "db_exists": Path(db_path_str).is_file(),
        }

    @app.get("/version", tags=["meta"])
    def version() -> dict[str, str]:
        """Return package version + build-time metadata (forensic trace)."""
        return {
            "version": __version__,
            "package": "bijotel",
        }

    @app.get("/chain", tags=["chain"], status_code=501)
    def chain_list() -> dict[str, str]:
        """Placeholder for chain listing. Implemented in v1.1.0."""
        raise HTTPException(
            status_code=501,
            detail="Chain listing endpoint coming in v1.1.0 (Day 6-7 of harvest plan).",
        )

    @app.get("/policy", tags=["policy"], status_code=501)
    def policy_state() -> dict[str, str]:
        """Placeholder for policy state. Implemented in v1.1.0."""
        raise HTTPException(
            status_code=501,
            detail="Policy state endpoint coming in v1.1.0.",
        )

    @app.get("/regression", tags=["regression"], status_code=501)
    def regression_scan() -> dict[str, str]:
        """Placeholder for regression scan. Implemented in v1.1.0."""
        raise HTTPException(
            status_code=501,
            detail="Regression scan endpoint coming in v1.1.0.",
        )

    return app


def _env_db_path() -> str:
    """Resolve DB path from BIJOTEL_DB_PATH env var, default "chain.db"."""
    return os.environ.get("BIJOTEL_DB_PATH", "chain.db")
