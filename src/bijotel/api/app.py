"""FastAPI app factory for ``bijotel serve``.

v1.1.0 ships real chain / policy / layers endpoints (Day 6 of the harvest
plan). The previous 501-placeholder routes from v1.0.0 are replaced by
the implementations in :mod:`bijotel.api.routes`.

Topology
========

* ``GET  /health``    — liveness, version, db_exists
* ``GET  /version``   — version + package
* ``GET  /chain``     — paginated list (filterable by since/until)
* ``GET  /chain/stats``     — aggregate stats
* ``GET  /chain/{seq}``     — one full entry (canonical body)
* ``POST /chain/verify``    — full or smoke verification
* ``GET  /policy/rules``    — list active rules + introspection
* ``POST /policy/evaluate`` — run a request through the engine
* ``GET  /layers``    — status of every BIJOTEL bijuterie

Mounting is done by :func:`create_app`. The host can pass a custom
:class:`bijotel.policy.engine.PolicyEngine` so dashboards reflect the
exact ruleset enforced in production. If omitted, a small default engine
is wired (see :func:`_default_policy_engine`) — having *some* rules is
much better than `/policy/rules → []`, which would look like a
misconfiguration.

This module is import-safe **only** when ``fastapi`` is installed. The
package's ``bijotel.api.__init__`` uses lazy ``__getattr__`` so the
base package still imports without the ``[api]`` extra.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "bijotel.api requires the [api] extra. Install with:\n"
        "    pip install bijotel[api]\n"
        "(this pulls in fastapi + uvicorn)"
    ) from e

from bijotel import __version__
from bijotel.api.auth import APIKeyMiddleware
from bijotel.policy.engine import PolicyEngine
from bijotel.policy.rules import (
    output_length_limit,
    pii_detection,
    prompt_pattern_deny,
)


def _default_policy_engine() -> PolicyEngine:
    """Return a small default engine — three warn-mode rules.

    Used when the host doesn't pass ``policy_engine=`` to ``create_app``.
    All rules are in WARN mode so the engine never denies — visiting
    ``/policy/evaluate`` won't surprise anyone with a 403-style decision.
    """
    return PolicyEngine(
        rules=[
            prompt_pattern_deny(mode="warn"),
            pii_detection(mode="warn"),
            output_length_limit(max_tokens=4096, mode="warn"),
        ]
    )


def create_app(
    db_path: str | Path = "chain.db",
    *,
    policy_engine: PolicyEngine | None = None,
    cors_origins: list[str] | None = None,
    api_key: str | None = None,
) -> FastAPI:
    """Build a fresh FastAPI app bound to ``db_path`` and ``policy_engine``.

    Args:
        db_path: Path to the BIJOTEL chain.db. Stored on the app and
            read by chain / layers endpoints. Not opened at construction
            time — endpoints open on demand so the server can boot
            before the chain is initialized.
        policy_engine: Optional :class:`PolicyEngine`. If ``None``, a
            small default warn-mode engine is wired (see
            :func:`_default_policy_engine`).
        cors_origins: List of allowed CORS origins for the dashboard.
            Defaults to ``["*"]`` for dev simplicity; set explicit origins
            in production (e.g. ``["https://dashboard.example.com"]``).
        api_key: Optional Bearer-token key. If ``None``, falls back to
            ``$BIJOTEL_API_KEY``. If still unset, the API runs without
            authentication (dev mode). When set, every endpoint except
            ``/health``, ``/version``, ``/docs``, ``/redoc`` and
            ``/openapi.json`` requires ``Authorization: Bearer <key>``.

    Returns:
        Configured :class:`FastAPI` instance.
    """
    db_path_str = str(db_path)

    app = FastAPI(
        title="BIJOTEL",
        description=(
            "Forensic-grade tamper-evident audit chain for LLM applications. "
            "HMAC-SHA256 chain + content-addressable storage + pre-call "
            "policy gate + regression detection."
        ),
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=[
            {"name": "meta", "description": "Health, version, liveness probes."},
            {"name": "chain", "description": "BIJOTEL HMAC chain (read-only)."},
            {"name": "policy", "description": "PolicyEngine introspection + dry-run."},
            {"name": "layers", "description": "Bijuterii catalog status."},
            {
                "name": "regression",
                "description": "Drift detection runs + history.",
            },
            {
                "name": "export",
                "description": "Portable signed JSON export + verification.",
            },
        ],
    )

    # ----- App state (per-instance, passed via `request.app.state`) -----
    app.state.db_path = db_path_str
    app.state.policy_engine = policy_engine or _default_policy_engine()

    # ----- Middleware -----
    # IMPORTANT: middleware order matters and FastAPI executes them in REVERSE
    # of registration order. We add CORS first so it sits on the OUTSIDE
    # (handles preflight before the auth check, which is what browsers
    # expect — preflight requests must succeed without credentials).
    app.add_middleware(
        APIKeyMiddleware,
        api_key=api_key,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
        allow_credentials=False,  # token-based, no cookies
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # ----- Meta routes inline (small, no payload structure) -----
    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str | bool]:
        """Liveness probe — 200 if the process is up.

        Reports ``db_exists`` honestly: the server boots before the chain
        is created, so ``db_exists=false`` is a legitimate transient state.
        """
        return {
            "status": "ok",
            "version": __version__,
            "db": db_path_str,
            "db_exists": Path(db_path_str).is_file(),
        }

    @app.get("/version", tags=["meta"])
    def version() -> dict[str, str]:
        """Return package version + name (forensic build trace)."""
        return {"version": __version__, "package": "bijotel"}

    # ----- Route modules (defer import so missing extras don't break /health) -----
    from bijotel.api.routes import chain as chain_routes
    from bijotel.api.routes import export as export_routes
    from bijotel.api.routes import layers as layers_routes
    from bijotel.api.routes import policy as policy_routes
    from bijotel.api.routes import regression as regression_routes

    app.include_router(chain_routes.router)
    app.include_router(policy_routes.router)
    app.include_router(layers_routes.router)
    app.include_router(regression_routes.router)
    app.include_router(export_routes.router)

    return app


def _env_db_path() -> str:
    """Resolve DB path from BIJOTEL_DB_PATH env var, default "chain.db"."""
    return os.environ.get("BIJOTEL_DB_PATH", "chain.db")
