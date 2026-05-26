"""Tests for deterministic-seed replay verification (v2.7.0).

Coverage:

- ``record_replay_context`` shape + determinism in both seed/no-seed paths.
- ``verify_replay`` match, mismatch (deterministic + non-deterministic
  paths produce *different* reasons), and pre-v2.7.0 entry handling.
- End-to-end: replay attrs flow through canonical body unchanged.
- CLI: happy path, mismatch path, output-file path, argument errors.
- REST: ``POST /replay/verify`` match + mismatch + 404 missing seq.
- Public API: top-level exports + ``ReplayResult`` shape.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.api.app import create_app
from bijotel.processors import HmacChainSpanProcessor
from bijotel.processors.canonical import span_to_canonical_dict
from bijotel.replay import ReplayResult, record_replay_context, verify_replay

SECRET = b"x" * 32


def _seal_one(db: Path, output: str, seed: int | None = 42) -> int:
    """Helper: seal a single span carrying replay attrs; return its seq."""
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db, secret_key=SECRET)
    )
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("rep") as span:
        span.set_attribute("gen_ai.request.model", "claude-haiku-4-5")
        attrs = record_replay_context(
            prompt="Hello world",
            output=output,
            model="claude-haiku-4-5",
            seed=seed,
            temperature=0.0,
        )
        for k, v in attrs.items():
            span.set_attribute(k, v)
    provider.shutdown()

    with sqlite3.connect(db) as conn:
        return int(conn.execute("SELECT seq FROM chain").fetchone()[0])


# ----------------------------------------------------------------------
# 1. record_replay_context
# ----------------------------------------------------------------------


def test_record_with_seed_sets_deterministic_true() -> None:
    attrs = record_replay_context(
        prompt="hi",
        output="hello",
        model="claude-haiku-4-5",
        seed=42,
        temperature=0.0,
    )
    assert attrs["bijotel.replay.deterministic"] is True
    assert attrs["bijotel.replay.seed"] == 42
    assert attrs["bijotel.replay.temperature"] == 0.0


def test_record_without_seed_omits_seed_key() -> None:
    """No seed → no seed attribute, and deterministic=False."""
    attrs = record_replay_context(
        prompt="hi",
        output="hello",
        model="claude-haiku-4-5",
    )
    assert attrs["bijotel.replay.deterministic"] is False
    assert "bijotel.replay.seed" not in attrs


def test_record_model_version_falls_back_to_model() -> None:
    attrs = record_replay_context(
        prompt="hi", output="hello", model="claude-haiku-4-5"
    )
    assert attrs["bijotel.replay.model_version"] == "claude-haiku-4-5"


def test_record_explicit_model_version() -> None:
    attrs = record_replay_context(
        prompt="hi",
        output="hello",
        model="claude",
        model_version="claude-haiku-4-5-20251001",
    )
    assert attrs["bijotel.replay.model_version"] == "claude-haiku-4-5-20251001"


def test_prompt_hash_deterministic_for_str() -> None:
    """Same string prompt → same hash, every time."""
    a = record_replay_context(prompt="hi", output="ok", model="m")
    b = record_replay_context(prompt="hi", output="ok", model="m")
    assert a["bijotel.replay.prompt_hash"] == b["bijotel.replay.prompt_hash"]


def test_prompt_hash_deterministic_for_messages_list() -> None:
    """List-of-dicts shape hashes via sorted-key JSON — order of insertion
    must not matter."""
    msgs = [{"role": "user", "content": "hi"}]
    a = record_replay_context(prompt=msgs, output="ok", model="m")
    msgs2 = [{"content": "hi", "role": "user"}]  # same dict, diff insertion order
    b = record_replay_context(prompt=msgs2, output="ok", model="m")
    assert a["bijotel.replay.prompt_hash"] == b["bijotel.replay.prompt_hash"]


def test_output_hash_is_sha256_of_utf8() -> None:
    """Output hash is the plain SHA-256 of the UTF-8 bytes."""
    attrs = record_replay_context(
        prompt="x", output="hello world", model="m"
    )
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert attrs["bijotel.replay.output_hash"] == expected


# ----------------------------------------------------------------------
# 2. verify_replay
# ----------------------------------------------------------------------


def test_verify_replay_match() -> None:
    attrs = record_replay_context(
        prompt="x", output="answer", model="m", seed=42
    )
    entry = {"attributes": attrs}
    r = verify_replay(entry, "answer")
    assert r.match is True
    assert r.reason is None


def test_verify_replay_mismatch_deterministic_reason_is_specific() -> None:
    attrs = record_replay_context(
        prompt="x", output="answer", model="m", seed=42
    )
    entry = {"attributes": attrs}
    r = verify_replay(entry, "DIFFERENT")
    assert r.match is False
    assert r.deterministic is True
    assert "SAME model version" in (r.reason or "")
    assert "m" in (r.reason or "")  # model version surfaced


def test_verify_replay_mismatch_nondeterministic_reason_is_specific() -> None:
    """When deterministic=False, the reason explains drift is expected."""
    attrs = record_replay_context(prompt="x", output="answer", model="m")
    entry = {"attributes": attrs}
    r = verify_replay(entry, "DIFFERENT")
    assert r.match is False
    assert r.deterministic is False
    assert "did not record a seed" in (r.reason or "")
    assert "expected" in (r.reason or "")


def test_verify_replay_missing_output_hash_returns_explicit_reason() -> None:
    """Pre-v2.7.0 entry (no replay attrs) — handled gracefully."""
    entry = {"attributes": {"gen_ai.request.model": "claude"}}
    r = verify_replay(entry, "anything")
    assert r.match is False
    assert r.original_hash is None
    assert "logged before v2.7.0" in (r.reason or "")


def test_replay_result_to_dict_roundtrips_json() -> None:
    """``ReplayResult`` serializes cleanly for the REST layer."""
    r = ReplayResult(
        match=True,
        original_hash="abc",
        replay_hash="abc",
        deterministic=True,
        model_version="claude",
        reason=None,
    )
    d = r.to_dict()
    assert json.loads(json.dumps(d)) == d


# ----------------------------------------------------------------------
# 3. Canonical body integration — attrs survive seal
# ----------------------------------------------------------------------


def test_replay_attrs_in_canonical_body() -> None:
    span = MagicMock()
    span.name = "test"
    span.kind.name = "CLIENT"
    span.attributes = record_replay_context(
        prompt="x", output="y", model="m", seed=42
    )
    span.status.status_code.name = "OK"
    span.status.description = None
    span.start_time = 1
    span.end_time = 2

    canonical = span_to_canonical_dict(span)
    body_attrs = canonical["attributes"]
    assert "bijotel.replay.output_hash" in body_attrs
    assert body_attrs["bijotel.replay.seed"] == 42
    assert body_attrs["bijotel.replay.deterministic"] is True


def test_replay_attributes_survive_seal_and_round_trip(tmp_path: Path) -> None:
    """End-to-end: record → seal → read back → verify_replay matches."""
    db = tmp_path / "chain.db"
    seq = _seal_one(db, output="the answer is 42", seed=42)

    with sqlite3.connect(db) as conn:
        body_raw = conn.execute(
            "SELECT canonical_body FROM chain WHERE seq = ?", (seq,)
        ).fetchone()[0]
    body_dict = json.loads(
        body_raw.decode("utf-8") if isinstance(body_raw, bytes) else body_raw
    )

    r = verify_replay(body_dict, "the answer is 42")
    assert r.match is True


# ----------------------------------------------------------------------
# 4. CLI
# ----------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run `python -m bijotel.cli.main` with given args; UTF-8 IO."""
    env = {**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "bijotel.cli.main", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_cli_replay_match(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    seq = _seal_one(db, output="the truth")
    out = _run_cli(
        "replay", "--db", str(db), "--seq", str(seq), "--output", "the truth"
    )
    assert out.returncode == 0, out.stderr
    assert "MATCH" in out.stdout


def test_cli_replay_mismatch_exit_code_3(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    seq = _seal_one(db, output="the truth")
    out = _run_cli(
        "replay", "--db", str(db), "--seq", str(seq), "--output", "a LIE"
    )
    assert out.returncode == 3
    assert "MISMATCH" in out.stdout


def test_cli_replay_output_file(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    seq = _seal_one(db, output="from file")
    out_file = tmp_path / "replay.txt"
    out_file.write_text("from file", encoding="utf-8")

    out = _run_cli(
        "replay", "--db", str(db), "--seq", str(seq),
        "--output-file", str(out_file),
    )
    assert out.returncode == 0, out.stderr
    assert "MATCH" in out.stdout


def test_cli_replay_missing_output_arg_exits_2(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seal_one(db, output="x")
    out = _run_cli("replay", "--db", str(db), "--seq", "1")
    assert out.returncode == 2
    assert "--output" in out.stderr


def test_cli_replay_unknown_seq_exits_1(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seal_one(db, output="x")
    out = _run_cli(
        "replay", "--db", str(db), "--seq", "999", "--output", "x"
    )
    assert out.returncode == 1
    assert "not found" in out.stderr


# ----------------------------------------------------------------------
# 5. REST endpoint
# ----------------------------------------------------------------------


def _make_test_client(db: Path) -> TestClient:
    app = create_app(db_path=str(db))
    return TestClient(app)


def test_api_replay_verify_match(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    seq = _seal_one(db, output="the truth")
    client = _make_test_client(db)
    r = client.post(
        "/replay/verify",
        json={"seq": seq, "replayed_output": "the truth"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["match"] is True
    assert body["original_hash"] == body["replay_hash"]
    assert body["deterministic"] is True


def test_api_replay_verify_mismatch_still_200(tmp_path: Path) -> None:
    """Mismatch is still a successful comparison — HTTP 200, body match=False."""
    db = tmp_path / "chain.db"
    seq = _seal_one(db, output="the truth")
    client = _make_test_client(db)
    r = client.post(
        "/replay/verify",
        json={"seq": seq, "replayed_output": "a LIE"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["match"] is False
    assert body["original_hash"] != body["replay_hash"]
    assert body["reason"] is not None


def test_api_replay_verify_404_unknown_seq(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _seal_one(db, output="x")
    client = _make_test_client(db)
    r = client.post(
        "/replay/verify",
        json={"seq": 99999, "replayed_output": "x"},
    )
    assert r.status_code == 404


# ----------------------------------------------------------------------
# 6. Public API
# ----------------------------------------------------------------------


def test_public_api_exports() -> None:
    import bijotel

    for name in ("ReplayResult", "record_replay_context", "verify_replay"):
        assert hasattr(bijotel, name), f"bijotel.{name} missing"
        assert name in bijotel.__all__
