"""``bijotel serve`` — FastAPI server entry point."""

from __future__ import annotations

import argparse
import os
import sys


def serve_cmd(args: argparse.Namespace) -> int:
    """Start the BIJOTEL HTTP API server.

    v1.0.0 ships a minimal FastAPI surface (``/health``, ``/version`` plus
    501-placeholder routes for chain / policy / regression). Full endpoints
    arrive in v1.1.0.

    Requires the ``[api]`` extra::

        pip install bijotel[api]

    Exit codes:
        0  — server shut down cleanly (SIGINT / SIGTERM).
        2  — missing fastapi / uvicorn (install bijotel[api]).
        3  — uvicorn raised at startup (port busy, bad host, etc.).
    """
    try:
        from bijotel.api.app import create_app
    except ImportError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("\nInstall the API extra:", file=sys.stderr)
        print("    pip install bijotel[api]", file=sys.stderr)
        return 2

    try:
        import uvicorn
    except ImportError:
        print(
            "ERROR: uvicorn not installed. Install with: pip install bijotel[api]",
            file=sys.stderr,
        )
        return 2

    db_path = args.db or os.environ.get("BIJOTEL_DB_PATH", "chain.db")
    serve_dashboard = getattr(args, "dashboard", False)
    app = create_app(db_path=db_path, serve_dashboard=serve_dashboard)

    api_base = "/api" if serve_dashboard else ""
    print(f"BIJOTEL serve: http://{args.host}:{args.port}  (db={db_path})")
    if serve_dashboard:
        print(f"  Dashboard: http://{args.host}:{args.port}/")
    print(f"  Docs:    http://{args.host}:{args.port}{api_base}/docs")
    print(f"  Health:  http://{args.host}:{args.port}{api_base}/health")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    except Exception as e:  # pragma: no cover - hard to simulate without real port
        print(f"ERROR: uvicorn startup failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    return 0
