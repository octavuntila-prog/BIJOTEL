"""Tests for Day 5 / v1.0.0 — `bijotel serve` + minimal FastAPI app."""

from __future__ import annotations

import importlib
import sys

import pytest

# Skip the entire module if FastAPI isn't installed (the [api] extra is opt-in).
fastapi = pytest.importorskip("fastapi", reason="bijotel[api] extra not installed")
TestClient = pytest.importorskip(
    "fastapi.testclient", reason="fastapi.testclient unavailable"
).TestClient


from bijotel import __version__  # noqa: E402  — after importorskip above
from bijotel.api import create_app  # noqa: E402

# === Module shape ===


def test_api_module_lazy_import() -> None:
    """`from bijotel.api import create_app` should resolve via __getattr__."""
    import bijotel.api as api_mod

    assert callable(api_mod.create_app)


def test_api_module_unknown_attribute_raises() -> None:
    import bijotel.api as api_mod

    with pytest.raises(AttributeError, match="no attribute"):
        api_mod.does_not_exist  # noqa: B018


def test_create_app_returns_fastapi() -> None:
    app = create_app()
    assert isinstance(app, fastapi.FastAPI)
    assert app.title == "BIJOTEL"
    assert app.version == __version__


def test_create_app_stores_db_path() -> None:
    app = create_app(db_path="/tmp/my-chain.db")
    assert app.state.db_path == "/tmp/my-chain.db"


def test_create_app_accepts_pathlib() -> None:
    from pathlib import Path

    app = create_app(db_path=Path("/var/data/chain.db"))
    # stringified for JSON-serializability downstream
    assert isinstance(app.state.db_path, str)


# === Endpoints ===


def test_health_endpoint_returns_ok() -> None:
    app = create_app(db_path="/tmp/nonexistent-chain.db")
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["db"] == "/tmp/nonexistent-chain.db"
    assert body["db_exists"] is False  # we used a fake path


def test_health_db_exists_flag_tracks_file(tmp_path) -> None:
    db = tmp_path / "chain.db"
    db.write_bytes(b"")  # create empty file
    app = create_app(db_path=str(db))
    client = TestClient(app)
    body = client.get("/health").json()
    assert body["db_exists"] is True


def test_version_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    r = client.get("/version")
    assert r.status_code == 200
    assert r.json() == {"version": __version__, "package": "bijotel"}


def test_chain_placeholder_returns_501() -> None:
    app = create_app()
    client = TestClient(app)
    r = client.get("/chain")
    assert r.status_code == 501
    assert "v1.1.0" in r.json()["detail"]


def test_policy_placeholder_returns_501() -> None:
    app = create_app()
    client = TestClient(app)
    r = client.get("/policy")
    assert r.status_code == 501


def test_regression_placeholder_returns_501() -> None:
    app = create_app()
    client = TestClient(app)
    r = client.get("/regression")
    assert r.status_code == 501


def test_openapi_docs_served() -> None:
    """OpenAPI / Swagger UI route is enabled (default config)."""
    app = create_app()
    client = TestClient(app)
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    # All v1.0.0 routes present
    paths = set(spec.get("paths", {}).keys())
    assert {"/health", "/version", "/chain", "/policy", "/regression"} <= paths


# === CLI integration ===


def test_serve_subparser_registered() -> None:
    """Parser exposes `bijotel serve` subcommand with expected args."""
    from bijotel.cli.main import _build_parser

    parser = _build_parser()
    # argparse stores subparsers internally; parse a valid invocation
    args = parser.parse_args(
        ["serve", "--host", "0.0.0.0", "--port", "9000", "--db", "/data/c.db"]
    )
    assert args.command == "serve"
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.db == "/data/c.db"


def test_serve_default_host_and_port() -> None:
    from bijotel.cli.main import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["serve"])
    assert args.host == "127.0.0.1"
    assert args.port == 8080
    assert args.db is None
    assert args.log_level == "info"


def test_serve_cmd_resolves_db_from_env(monkeypatch, tmp_path) -> None:
    """`bijotel serve --db <unset>` falls back to BIJOTEL_DB_PATH env var."""
    db = tmp_path / "env-chain.db"
    monkeypatch.setenv("BIJOTEL_DB_PATH", str(db))

    # We don't actually want uvicorn.run() to bind a port — patch it out.
    from bijotel.cli import commands as cmd_mod

    called_with: dict = {}

    def fake_uvicorn_run(app, host, port, log_level):  # noqa: ARG001
        called_with["host"] = host
        called_with["port"] = port
        called_with["app"] = app

    monkeypatch.setattr(
        cmd_mod, "__name__", cmd_mod.__name__
    )  # no-op; just to keep monkeypatch happy

    # Replace uvicorn at the module the import lives in
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)

    import argparse as ap

    args = ap.Namespace(host="127.0.0.1", port=8765, db=None, log_level="info")
    rc = cmd_mod.serve_cmd(args)
    assert rc == 0
    assert called_with["host"] == "127.0.0.1"
    assert called_with["port"] == 8765
    # app.state.db_path should reflect env var
    assert called_with["app"].state.db_path == str(db)


def test_serve_cmd_without_fastapi_graceful(monkeypatch, capsys) -> None:
    """If fastapi import fails, serve_cmd exits 2 with remediation message."""
    import builtins

    from bijotel.cli import commands as cmd_mod

    # Pop cached api module so the import inside serve_cmd actually hits __import__
    saved = sys.modules.pop("bijotel.api.app", None)
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "bijotel.api.app":
            raise ImportError("simulated: fastapi not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    try:
        import argparse as ap

        args = ap.Namespace(
            host="127.0.0.1", port=8080, db="chain.db", log_level="info"
        )
        rc = cmd_mod.serve_cmd(args)
        assert rc == 2
        captured = capsys.readouterr()
        assert "bijotel[api]" in captured.err
    finally:
        # Restore cached module so subsequent tests see real implementation
        if saved is not None:
            sys.modules["bijotel.api.app"] = saved
        importlib.invalidate_caches()
