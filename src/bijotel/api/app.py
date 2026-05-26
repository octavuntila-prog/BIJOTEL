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

import logging
import os
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.staticfiles import StaticFiles
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

_LOG = logging.getLogger("bijotel.api")


def _default_policy_engine() -> PolicyEngine:
    """Return a full default engine in WARN mode (v1.9.1+).

    Used when the host doesn't pass ``policy_engine=`` to ``create_app``.
    Every rule that ships with the package is wired here — F11 prompt
    patterns, PII detection, output length, AST safety (if the
    ``[ast]`` extra is installed), and routing recommendation.

    All rules WARN-only so the engine never denies a default
    ``/policy/evaluate`` call. Hosts that want a custom subset pass
    ``policy_engine=`` to ``create_app`` explicitly.

    The graceful-import pattern lets a minimal install (no
    ``[ast]`` extra) still build the engine — tree-sitter-dependent
    rules are silently skipped. Routing is pure Python, always
    available.
    """
    rules = [
        prompt_pattern_deny(mode="warn", use_defaults=True),
        pii_detection(mode="warn"),
        output_length_limit(max_tokens=4096, mode="warn"),
    ]

    # AST safety — requires [ast] extra (tree-sitter + tree-sitter-bash).
    try:
        from bijotel.layers.ast_safety import ast_safety_check

        rules.append(
            ast_safety_check(languages=("python", "bash"), mode="warn")
        )
    except ImportError:
        pass

    # Routing recommendation — pure Python, always available.
    # v2.0.4+: respect BIJOTEL_MODELS env var so hosts can scope
    # the recommender to their actual fleet. Without it, the default
    # registry (Haiku + Sonnet + Opus + GPT-4o family) recommends
    # gpt-4o-mini for every prompt because it's the cheapest entry
    # — actionable only on fleets that include OpenAI, noise on
    # Anthropic-only deployments like GENA.
    #
    # Format: comma-separated model names, e.g.
    #   BIJOTEL_MODELS="claude-haiku-4-5-20251001,claude-sonnet-4-20250514"
    # Unknown model names are silently dropped (no crash on typos).
    try:
        from bijotel.layers.routing import (
            DEFAULT_MODELS,
            ModelRegistry,
            ParetoRouter,
            routing_recommendation,
        )

        models_env = os.environ.get("BIJOTEL_MODELS", "").strip()
        if models_env:
            wanted = [m.strip() for m in models_env.split(",") if m.strip()]
            scoped = {m: DEFAULT_MODELS[m] for m in wanted if m in DEFAULT_MODELS}
            if scoped:
                registry = ModelRegistry(scoped)
                rules.append(
                    routing_recommendation(
                        registry=registry,
                        router=ParetoRouter(registry=registry),
                        mode="warn",
                    )
                )
            else:
                # All names invalid — fall back to default rather than skip
                rules.append(routing_recommendation(mode="warn"))
        else:
            rules.append(routing_recommendation(mode="warn"))
    except Exception:
        # Defensive: if a future routing rule constructor changes,
        # don't crash the whole engine.
        pass

    return PolicyEngine(rules=rules)


def create_app(
    db_path: str | Path = "chain.db",
    *,
    policy_engine: PolicyEngine | None = None,
    containment_guard: Any | None = None,
    cors_origins: list[str] | None = None,
    api_key: str | None = None,
    serve_dashboard: bool = False,
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
        containment_guard: Optional pre-built :class:`ContainmentGuard`
            (Combo D). When set, ``/containment/evaluate`` uses it
            verbatim. When ``None``, the endpoint builds a guard
            lazily on first call from ``policy_engine`` + an
            ``ASTSafetyChecker`` (if the ``[ast]`` extra is installed).
            Hosts that need a custom AST config or a ``chain_writer``
            should pass their own guard here.
        cors_origins: List of allowed CORS origins for the dashboard.
            Defaults to ``["*"]`` for dev simplicity; set explicit origins
            in production (e.g. ``["https://dashboard.example.com"]``).
        api_key: Optional Bearer-token key. If ``None``, falls back to
            ``$BIJOTEL_API_KEY``. If still unset, the API runs without
            authentication (dev mode). When set, every endpoint except
            ``/health``, ``/version``, ``/docs``, ``/redoc`` and
            ``/openapi.json`` requires ``Authorization: Bearer <key>``.
        serve_dashboard: If ``True``, mount the prebuilt React dashboard
            at ``/`` (from ``src/bijotel/dashboard_dist/``) AND shift all
            API routes to the ``/api/*`` prefix so they don't collide
            with the SPA's client-side routes. Default ``False`` keeps
            v1.1.0 behavior — routes at root, no dashboard.

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
            {
                "name": "containment",
                "description": "Combo D three-question gate "
                "(permitted? safe? sealed?).",
            },
            {
                "name": "consensus",
                "description": "Multi-LLM voting (Bijuteria #9) — "
                "N-version programming for hallucination resistance.",
            },
            {
                "name": "energy",
                "description": "AI energy + carbon accounting (Bijuteria #3).",
            },
        ],
    )

    # ----- App state (per-instance, passed via `request.app.state`) -----
    app.state.db_path = db_path_str
    app.state.policy_engine = policy_engine or _default_policy_engine()
    # containment_guard is lazy by default: the /containment/evaluate
    # handler builds one on first call when None (cached afterwards).
    app.state.containment_guard = containment_guard

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

    # ----- Meta routes (helpers so we can mount them under / AND /api) -----
    def _register_meta(target) -> None:
        @target.get("/health", tags=["meta"])
        def health() -> dict[str, str | bool]:
            """Liveness probe — 200 if the process is up."""
            return {
                "status": "ok",
                "version": __version__,
                "db": db_path_str,
                "db_exists": Path(db_path_str).is_file(),
            }

        @target.get("/version", tags=["meta"])
        def version() -> dict[str, str]:
            """Return package version + name (forensic build trace)."""
            return {"version": __version__, "package": "bijotel"}

    # Always register at root for kubernetes / load balancer liveness probes.
    _register_meta(app)

    # ----- Route modules (defer import so missing extras don't break /health) -----
    from bijotel.api.routes import archive as archive_routes
    from bijotel.api.routes import chain as chain_routes
    from bijotel.api.routes import consensus as consensus_routes
    from bijotel.api.routes import containment as containment_routes
    from bijotel.api.routes import energy as energy_routes
    from bijotel.api.routes import export as export_routes
    from bijotel.api.routes import integrity as integrity_routes
    from bijotel.api.routes import layers as layers_routes
    from bijotel.api.routes import policy as policy_routes
    from bijotel.api.routes import regression as regression_routes
    from bijotel.api.routes import replay as replay_routes

    route_modules = [
        chain_routes,
        policy_routes,
        layers_routes,
        regression_routes,
        export_routes,
        containment_routes,
        consensus_routes,
        energy_routes,
        archive_routes,
        replay_routes,
        integrity_routes,
    ]

    if serve_dashboard:
        # In --dashboard mode, group all API routes under /api/* so the
        # static SPA can own /. The dashboard's client.js already uses
        # `const API_BASE = '/api'` so this is the URL it expects.
        # Meta routes are duplicated under /api so the dashboard fetch
        # for liveness uses the same prefix as everything else.
        api_router = APIRouter(prefix="/api")
        _register_meta(api_router)
        for mod in route_modules:
            api_router.include_router(mod.router)
        app.include_router(api_router)
    else:
        # v1.1.0 behavior: routes at root (no prefix). Backward compatible.
        for mod in route_modules:
            app.include_router(mod.router)

    # ----- Optional static dashboard mount -----
    if serve_dashboard:
        # Resolve dashboard_dist next to this module: src/bijotel/dashboard_dist
        dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard_dist"
        if dashboard_dir.is_dir() and (dashboard_dir / "index.html").is_file():
            # Serve the asset bundles directly. We DON'T mount StaticFiles
            # at "/" alone because StaticFiles + html=True returns 404 for
            # arbitrary deep paths (e.g. /system) — it does not fall back
            # to index.html the way single-page apps require. Instead:
            #   * mount /assets/*  →  hashed JS/CSS chunks
            #   * register a catch-all GET that returns index.html for any
            #     path not handled by an API route or the assets mount.
            from fastapi.responses import FileResponse

            app.mount(
                "/assets",
                StaticFiles(directory=str(dashboard_dir / "assets")),
                name="dashboard-assets",
            )

            index_html_path = dashboard_dir / "index.html"

            # Return-type annotations intentionally omitted on these SPA
            # routes. With `from __future__ import annotations`, Pydantic
            # 2.9 (the pin on GENA) cannot forward-resolve `FileResponse`
            # at schema-gen time even when include_in_schema=False. Works
            # on local Pydantic 2.10+, fails on 2.9. Until we bump the
            # GENA pin to >=2.10, the safe form is no annotation.
            @app.get("/", include_in_schema=False)
            def _spa_root():  # noqa: ANN201
                return FileResponse(str(index_html_path))

            # Catch-all for client-side router. include_in_schema=False so
            # this doesn't appear in /openapi.json. Routes win before this
            # catch-all via FastAPI's matcher, so /api/chain still routes
            # to the API instead of falling through to the SPA.
            @app.get("/{spa_path:path}", include_in_schema=False)
            def _spa_catchall(spa_path: str):  # noqa: ANN201
                return FileResponse(str(index_html_path))
        else:
            _LOG.warning(
                "serve_dashboard=True but no dashboard bundle at %s. "
                "Build it with: cd src/bijotel/dashboard && npm run build",
                dashboard_dir,
            )

    return app


def _env_db_path() -> str:
    """Resolve DB path from BIJOTEL_DB_PATH env var, default "chain.db"."""
    return os.environ.get("BIJOTEL_DB_PATH", "chain.db")
