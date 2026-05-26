"""Tests for ``/keygen``, ``/archive``, ``/verify-continuity`` (v2.3.0).

End-to-end coverage of the REST-API surface that previously lived
CLI-only — the v2.1.0 + v2.2.0 features that the internal audit
2026-05-26 flagged as missing from `bijotel serve`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from bijotel.api import create_app  # noqa: E402
from bijotel.processors import HmacChainSpanProcessor  # noqa: E402

SECRET = b"x" * 32
SECRET_HEX = SECRET.hex()


@pytest.fixture
def populated_chain_db(tmp_path: Path) -> Path:
    """Seed a chain.db with 8 spans for archive/continuity tests."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("api-archive-test")
    for i in range(8):
        with tracer.start_as_current_span(f"span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 10)
            span.set_attribute("gen_ai.usage.output_tokens", 5)
    provider.shutdown()
    return db


# ───────────────────────── /keygen ─────────────────────────


def test_keygen_returns_pubkey_and_fingerprint(tmp_path: Path) -> None:
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    out = tmp_path / "keys"
    r = client.post("/keygen", json={"output_dir": str(out)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "BEGIN PUBLIC KEY" in body["public_key_pem"]
    assert len(body["fingerprint"]) == 16
    int(body["fingerprint"], 16)  # raises if not hex
    assert (out / "bijotel_private.pem").exists()
    assert (out / "bijotel_public.pem").exists()


def test_keygen_refuses_overwrite_without_force(tmp_path: Path) -> None:
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    out = tmp_path / "keys"
    client.post("/keygen", json={"output_dir": str(out)})
    r2 = client.post("/keygen", json={"output_dir": str(out)})
    assert r2.status_code == 409


def test_keygen_force_rotates_key(tmp_path: Path) -> None:
    app = create_app(db_path="/tmp/no-such.db")
    client = TestClient(app)
    out = tmp_path / "keys"
    fp1 = client.post("/keygen", json={"output_dir": str(out)}).json()["fingerprint"]
    fp2 = client.post(
        "/keygen", json={"output_dir": str(out), "force": True}
    ).json()["fingerprint"]
    assert fp1 != fp2


# ───────────────────────── /archive ─────────────────────────


def test_archive_requires_hmac_secret_env(
    populated_chain_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No BIJOTEL_HMAC_SECRET → 400 with helpful message."""
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)
    app = create_app(db_path=str(populated_chain_db))
    client = TestClient(app)
    r = client.post("/archive", json={
        "output_path": str(tmp_path / "arc.db"),
        "before_seq": 5,
        "dry_run": True,
    })
    assert r.status_code == 400
    assert "BIJOTEL_HMAC_SECRET" in r.json()["detail"]


def test_archive_dry_run(
    populated_chain_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(populated_chain_db))
    client = TestClient(app)
    archive_path = tmp_path / "arc_dry.db"
    r = client.post("/archive", json={
        "output_path": str(archive_path),
        "before_seq": 5,
        "dry_run": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["archived_count"] == 4  # seq 1..4
    assert body["first_seq"] == 1
    assert body["last_seq"] == 4
    assert body["main_remaining_count"] == 4
    assert not archive_path.exists()  # dry-run wrote nothing


def test_archive_apply_creates_db_and_trims_source(
    populated_chain_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(populated_chain_db))
    client = TestClient(app)
    archive_path = tmp_path / "arc_real.db"
    r = client.post("/archive", json={
        "output_path": str(archive_path),
        "before_seq": 5,
        "dry_run": False,
    })
    assert r.status_code == 200, r.text
    assert archive_path.exists()
    # Source now has only seq 5..8
    import sqlite3
    with sqlite3.connect(populated_chain_db) as conn:
        rows = conn.execute("SELECT MIN(seq), MAX(seq), COUNT(*) FROM chain").fetchone()
    assert rows == (5, 8, 4)


def test_archive_rejects_both_filters(
    populated_chain_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(populated_chain_db))
    client = TestClient(app)
    r = client.post("/archive", json={
        "output_path": str(tmp_path / "x.db"),
        "before_seq": 5,
        "before_iso": "2026-05-20",
        "dry_run": True,
    })
    assert r.status_code == 400
    assert "exactly one" in r.json()["detail"].lower()


def test_archive_with_sign_key_writes_sidecar(
    populated_chain_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(populated_chain_db))
    client = TestClient(app)
    # Generate a key via /keygen
    keys_dir = tmp_path / "k"
    keygen_resp = client.post("/keygen", json={"output_dir": str(keys_dir)}).json()
    priv = keygen_resp["private_key_path"]

    archive_path = tmp_path / "arc_signed.db"
    r = client.post("/archive", json={
        "output_path": str(archive_path),
        "before_seq": 5,
        "sign_key_path": priv,
        "dry_run": False,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["segment_json_path"] is not None
    assert Path(body["segment_json_path"]).exists()


# ───────────────────────── /verify-continuity ─────────────────────────


def test_continuity_two_segments(
    populated_chain_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archive then verify continuity across (archive, live)."""
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(populated_chain_db))
    client = TestClient(app)
    archive_path = tmp_path / "arc_cont.db"
    client.post("/archive", json={
        "output_path": str(archive_path),
        "before_seq": 5,
        "dry_run": False,
    })
    r = client.post("/verify-continuity", json={
        "db_paths": [str(archive_path), str(populated_chain_db)],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert len(body["segments"]) == 2
    assert all(s["valid"] for s in body["segments"])
    assert len(body["boundaries"]) == 1
    assert body["boundaries"][0]["matches"] is True


def test_continuity_single_segment_no_boundaries() -> None:
    app = create_app(db_path="/tmp/x.db")
    client = TestClient(app)
    r = client.post("/verify-continuity", json={"db_paths": ["/tmp/does_not_exist.db"]})
    body = r.json()
    # Missing file → invalid, but endpoint returns 200 with detail.
    assert r.status_code == 200
    assert body["valid"] is False
    assert body["segments"][0]["reason"] == "file not found"


# ───────────────────────── /chain/verify with range params ─────────────────────────


def test_chain_verify_with_range(
    populated_chain_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /chain/verify accepts seq_start/seq_end and validates a slice."""
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(populated_chain_db))
    client = TestClient(app)
    r = client.post(
        "/chain/verify",
        json={"full": True, "seq_start": 2, "seq_end": 6},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True


def test_chain_verify_last_n(
    populated_chain_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    app = create_app(db_path=str(populated_chain_db))
    client = TestClient(app)
    r = client.post("/chain/verify", json={"full": True, "last_n": 3})
    assert r.status_code == 200, r.text
    assert r.json()["valid"] is True
