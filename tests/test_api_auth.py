"""Tests for the Bearer-token auth middleware (Day 7 / v1.1.0)."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from bijotel.api import create_app  # noqa: E402

API_KEY = "test-secret-key-12345"


# ───────────────────────── no key → no auth ─────────────────────────


def test_no_auth_when_key_unset(monkeypatch) -> None:
    """With no api_key arg and no env var, all endpoints are open."""
    monkeypatch.delenv("BIJOTEL_API_KEY", raising=False)
    app = create_app(db_path="/tmp/no-db.db")
    c = TestClient(app)
    # /layers is auth-able; without a key, must respond 200 directly
    r = c.get("/layers")
    assert r.status_code == 200


# ───────────────────────── key → auth required ─────────────────────────


def test_auth_required_when_key_set() -> None:
    """When the api_key is passed, /layers without header → 401."""
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    r = c.get("/layers")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"
    assert "Authorization" in r.json()["detail"]


def test_auth_correct_key_passes() -> None:
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    r = c.get("/layers", headers={"Authorization": f"Bearer {API_KEY}"})
    assert r.status_code == 200


def test_auth_wrong_key_401() -> None:
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    r = c.get("/layers", headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid API key."


def test_auth_malformed_header_401() -> None:
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    # Missing 'Bearer ' prefix
    r = c.get("/layers", headers={"Authorization": API_KEY})
    assert r.status_code == 401


def test_auth_bearer_lowercase_accepted() -> None:
    """Header schemes are case-insensitive per RFC; 'bearer' should work too."""
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    r = c.get("/layers", headers={"Authorization": f"bearer {API_KEY}"})
    assert r.status_code == 200


def test_auth_env_var_takes_effect(monkeypatch) -> None:
    """api_key=None falls back to BIJOTEL_API_KEY env var."""
    monkeypatch.setenv("BIJOTEL_API_KEY", API_KEY)
    app = create_app(db_path="/tmp/no-db.db")  # api_key omitted
    c = TestClient(app)
    r = c.get("/layers")
    assert r.status_code == 401  # auth required by env
    r2 = c.get("/layers", headers={"Authorization": f"Bearer {API_KEY}"})
    assert r2.status_code == 200


def test_auth_empty_env_var_is_no_op(monkeypatch) -> None:
    """BIJOTEL_API_KEY='' (empty) is treated as 'unset', no auth required."""
    monkeypatch.setenv("BIJOTEL_API_KEY", "")
    app = create_app(db_path="/tmp/no-db.db")
    c = TestClient(app)
    r = c.get("/layers")
    assert r.status_code == 200


# ───────────────────────── public-path bypass ─────────────────────────


def test_health_bypasses_auth() -> None:
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    r = c.get("/health")
    assert r.status_code == 200


def test_version_bypasses_auth() -> None:
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    r = c.get("/version")
    assert r.status_code == 200


def test_docs_bypasses_auth() -> None:
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    r = c.get("/docs")
    assert r.status_code == 200


def test_openapi_json_bypasses_auth() -> None:
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    r = c.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    # Sanity: the v1.1.0 endpoint set is in the spec
    paths = set(spec["paths"].keys())
    assert {
        "/regression/run",
        "/regression/latest",
        "/regression/history",
        "/export",
        "/export/verify",
    } <= paths


def test_protected_endpoints_all_401_without_key() -> None:
    """Belt-and-braces: every non-public endpoint returns 401 sans header."""
    app = create_app(db_path="/tmp/no-db.db", api_key=API_KEY)
    c = TestClient(app)
    for path, method in [
        ("/chain", "get"),
        ("/chain/stats", "get"),
        ("/chain/1", "get"),
        ("/chain/verify", "post"),
        ("/policy/rules", "get"),
        ("/policy/evaluate", "post"),
        ("/layers", "get"),
        ("/regression/latest", "get"),
        ("/regression/history", "get"),
        ("/regression/run", "post"),
        ("/export", "post"),
    ]:
        fn = getattr(c, method)
        r = fn(path) if method == "get" else fn(path, json={})
        assert r.status_code == 401, f"{method.upper()} {path} expected 401, got {r.status_code}"
