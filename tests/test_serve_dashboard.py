"""Tests for Day 12 / v1.4.0 — ``bijotel serve --dashboard`` integration."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from bijotel.api import create_app  # noqa: E402

# ───────────────────────── default mode (backward compat) ─────────────────────────


def test_default_mode_routes_at_root() -> None:
    """Without --dashboard, API routes stay at root (v1.1.0 behavior)."""
    app = create_app(db_path="/tmp/x.db", serve_dashboard=False)
    client = TestClient(app)

    # /health works at root
    assert client.get("/health").status_code == 200

    # /chain answers 503 (db missing) — proves the route IS mounted at root
    r = client.get("/chain")
    assert r.status_code == 503
    assert "Chain DB not found" in r.json()["detail"]

    # /api/chain does NOT exist in default mode
    assert client.get("/api/chain").status_code == 404


def test_default_mode_no_dashboard_mount() -> None:
    """No --dashboard → / is not a SPA fallback (returns 404)."""
    app = create_app(db_path="/tmp/x.db", serve_dashboard=False)
    client = TestClient(app)
    r = client.get("/")
    # No route at /, no static mount → 404
    assert r.status_code == 404


# ───────────────────────── --dashboard mode ─────────────────────────


def test_dashboard_mode_api_under_prefix() -> None:
    """With --dashboard, API routes live at /api/*."""
    app = create_app(db_path="/tmp/x.db", serve_dashboard=True)
    client = TestClient(app)

    # /api/health works
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # /api/chain returns 503 (db missing) — proves the prefix mount works
    r = client.get("/api/chain")
    assert r.status_code == 503

    # /api/policy/rules → 200 (policy engine default)
    r = client.get("/api/policy/rules")
    assert r.status_code == 200
    assert r.json()["total"] >= 1


def test_dashboard_mode_root_health_still_works() -> None:
    """Root /health stays available for k8s liveness probes in --dashboard."""
    app = create_app(db_path="/tmp/x.db", serve_dashboard=True)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_dashboard_mode_root_no_chain_route() -> None:
    """In --dashboard mode, /chain (no prefix) is NOT routed to the API."""
    app = create_app(db_path="/tmp/x.db", serve_dashboard=True)
    client = TestClient(app)
    # /chain would fall through to static mount; without dashboard files
    # built, mount may not exist → 404. With files, would serve index.html.
    r = client.get("/chain")
    # Either no-bundle 404 OR served the SPA HTML — but NOT the JSON 503
    # the API would return. The body must not contain BIJOTEL's chain
    # error JSON shape.
    if r.status_code == 200:
        assert "Chain DB not found" not in r.text
    else:
        assert r.status_code == 404


def test_dashboard_mode_serves_index_when_bundle_present(tmp_path) -> None:
    """If src/bijotel/dashboard_dist/index.html exists, GET / returns the SPA."""
    from pathlib import Path

    pkg_root = Path("src/bijotel").resolve()
    bundle = pkg_root / "dashboard_dist" / "index.html"
    if not bundle.is_file():
        pytest.skip(
            "dashboard not built; run `cd src/bijotel/dashboard && npm run build`"
        )

    app = create_app(db_path="/tmp/x.db", serve_dashboard=True)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    # SPA HTML starts with the standard doctype
    assert "<!doctype html>" in r.text.lower()
    # Vite injects a module script for the bundle
    assert "/assets/" in r.text or "<script" in r.text


def test_dashboard_mode_spa_fallback_on_unknown_path(tmp_path) -> None:
    """Unknown frontend routes fall back to index.html (React Router needs this)."""
    from pathlib import Path

    bundle = Path("src/bijotel/dashboard_dist/index.html").resolve()
    if not bundle.is_file():
        pytest.skip("dashboard not built")

    app = create_app(db_path="/tmp/x.db", serve_dashboard=True)
    client = TestClient(app)
    # /system is a SPA route; should return the index.html for client-side routing
    r = client.get("/system")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text.lower()


# ───────────────────────── CLI integration ─────────────────────────


def test_cli_dashboard_flag_parsed() -> None:
    from bijotel.cli.main import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["serve", "--dashboard"])
    assert args.command == "serve"
    assert args.dashboard is True


def test_cli_dashboard_default_false() -> None:
    from bijotel.cli.main import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["serve"])
    assert args.dashboard is False


def test_cli_serve_cmd_propagates_dashboard_flag(monkeypatch, tmp_path) -> None:
    """``bijotel serve --dashboard`` builds an app with serve_dashboard=True."""
    from bijotel.api import app as app_mod
    from bijotel.cli import commands as cmd_mod

    real_create_app = app_mod.create_app
    captured: dict = {}

    def fake_create_app(db_path, **kw):
        captured["db_path"] = db_path
        captured["serve_dashboard"] = kw.get("serve_dashboard", False)
        # Call the *original* create_app via the saved reference so we
        # don't recurse through the monkeypatched binding.
        return real_create_app(db_path=db_path, serve_dashboard=False)

    monkeypatch.setattr(app_mod, "create_app", fake_create_app)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)

    import argparse as ap

    args = ap.Namespace(
        host="127.0.0.1",
        port=8765,
        db=str(tmp_path / "x.db"),
        log_level="info",
        dashboard=True,
    )
    rc = cmd_mod.serve_cmd(args)
    assert rc == 0
    assert captured["serve_dashboard"] is True


# ───────────────────────── auth + dashboard interaction ─────────────────────────


def test_dashboard_static_assets_bypass_auth() -> None:
    """SPA bundle paths (/, /assets/*) must render without Bearer token."""
    from pathlib import Path

    if not Path("src/bijotel/dashboard_dist/index.html").is_file():
        pytest.skip("dashboard not built")
    app = create_app(
        db_path="/tmp/x.db",
        serve_dashboard=True,
        api_key="my-key",
    )
    client = TestClient(app)
    # / must NOT require auth
    assert client.get("/").status_code == 200
    # /api/health must NOT require auth (k8s probes, public allow-list)
    assert client.get("/api/health").status_code == 200
    # But /api/layers DOES require auth
    assert client.get("/api/layers").status_code == 401
    assert (
        client.get("/api/layers", headers={"Authorization": "Bearer my-key"}).status_code
        == 200
    )
