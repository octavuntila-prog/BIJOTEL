"""Tests for the cross-ecosystem view (v2.13.0)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bijotel.cross_view import (
    CrossEcosystemView,
    load_chain_stats_from_db,
    load_chain_stats_from_export,
)

# ─── helpers: build a minimal chain.db on disk ──────────────────────


def _make_chain_db(path: Path, entries: list[dict]) -> None:
    """Build a chain.db with the minimum schema cross_view reads.

    `entries` is a list of dicts; each gets one row. The ``canonical_body``
    JSON is built from optional ``provider`` and ``model`` keys.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE chain (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_ns INTEGER NOT NULL,
            trace_id TEXT NOT NULL,
            span_id TEXT NOT NULL,
            span_name TEXT,
            span_kind TEXT,
            canonical_body BLOB NOT NULL,
            canonical_hash TEXT,
            prev_hash TEXT,
            hmac_hash TEXT,
            semantic_body_hash TEXT
        )
        """
    )
    for i, e in enumerate(entries):
        body = {"attributes": {}}
        if "provider" in e:
            body["attributes"]["gen_ai.provider.name"] = e["provider"]
        if "model" in e:
            body["attributes"]["gen_ai.request.model"] = e["model"]
        conn.execute(
            "INSERT INTO chain (timestamp_ns, trace_id, span_id, canonical_body) "
            "VALUES (?, ?, ?, ?)",
            (
                e.get("ts", 1_700_000_000_000_000_000 + i * 1_000_000),
                f"trace_{i}",
                f"span_{i}",
                json.dumps(body),
            ),
        )
    conn.commit()
    conn.close()


# ─── loader tests ───────────────────────────────────────────────────


def test_load_db_empty(tmp_path):
    db = tmp_path / "empty.db"
    _make_chain_db(db, [])
    stats = load_chain_stats_from_db("empty", str(db))
    assert stats.entries == 0
    assert stats.providers == set()
    assert stats.models == {}
    assert stats.first_timestamp_ns is None


def test_load_db_with_entries(tmp_path):
    db = tmp_path / "live.db"
    _make_chain_db(db, [
        {"provider": "anthropic", "model": "claude-haiku-4-5", "ts": 100},
        {"provider": "anthropic", "model": "claude-haiku-4-5", "ts": 200},
        {"provider": "xai", "model": "grok-3-mini", "ts": 300},
    ])
    stats = load_chain_stats_from_db("GENA", str(db))
    assert stats.entries == 3
    assert stats.providers == {"anthropic", "xai"}
    assert stats.models == {"claude-haiku-4-5": 2, "grok-3-mini": 1}
    assert stats.first_timestamp_ns == 100
    assert stats.last_timestamp_ns == 300


def test_load_db_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_chain_stats_from_db("missing", str(tmp_path / "nope.db"))


def test_load_export_format(tmp_path):
    export = tmp_path / "chain.json"
    export.write_text(json.dumps({
        "entries": [
            {
                "timestamp_ns": 100,
                "canonical_body": {
                    "attributes": {
                        "gen_ai.provider.name": "openai",
                        "gen_ai.request.model": "gpt-4o-mini",
                    }
                },
            },
            {
                "timestamp_ns": 200,
                "canonical_body": json.dumps({
                    "attributes": {
                        "gen_ai.provider.name": "openai",
                        "gen_ai.request.model": "gpt-4o-mini",
                    }
                }),
            },
        ]
    }))
    stats = load_chain_stats_from_export("ARA", str(export))
    assert stats.entries == 2
    assert stats.providers == {"openai"}
    assert stats.models == {"gpt-4o-mini": 2}
    assert stats.first_timestamp_ns == 100
    assert stats.last_timestamp_ns == 200


def test_load_export_empty(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"entries": []}))
    stats = load_chain_stats_from_export("empty", str(p))
    assert stats.entries == 0


# ─── CrossEcosystemView tests ───────────────────────────────────────


def test_view_add_two_chains_summary(tmp_path):
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    _make_chain_db(db_a, [
        {"provider": "anthropic", "model": "claude-haiku-4-5", "ts": 100},
        {"provider": "anthropic", "model": "claude-sonnet-4", "ts": 200},
    ])
    _make_chain_db(db_b, [
        {"provider": "openai", "model": "gpt-4o-mini", "ts": 150},
    ])

    view = CrossEcosystemView()
    view.add_chain("GENA", db_path=str(db_a))
    view.add_chain("ARA", db_path=str(db_b))

    s = view.summary()
    assert s["ecosystems"] == 2
    assert s["total_entries"] == 3
    assert s["total_providers"] == ["anthropic", "openai"]
    assert s["earliest_timestamp_ns"] == 100
    assert s["latest_timestamp_ns"] == 200
    assert s["per_ecosystem"]["GENA"]["entries"] == 2
    assert s["per_ecosystem"]["ARA"]["entries"] == 1


def test_view_empty():
    view = CrossEcosystemView()
    s = view.summary()
    assert s["ecosystems"] == 0
    assert s["total_entries"] == 0
    assert s["total_providers"] == []
    assert s["earliest_timestamp_ns"] is None


def test_view_single_chain(tmp_path):
    db = tmp_path / "only.db"
    _make_chain_db(db, [{"provider": "anthropic", "model": "c", "ts": 1}])
    view = CrossEcosystemView()
    view.add_chain("solo", db_path=str(db))
    s = view.summary()
    assert s["ecosystems"] == 1
    assert s["total_entries"] == 1


def test_view_duplicate_name_rejected(tmp_path):
    db = tmp_path / "x.db"
    _make_chain_db(db, [])
    view = CrossEcosystemView()
    view.add_chain("X", db_path=str(db))
    with pytest.raises(ValueError, match="already added"):
        view.add_chain("X", db_path=str(db))


def test_view_add_requires_exactly_one_source(tmp_path):
    db = tmp_path / "x.db"
    _make_chain_db(db, [])
    view = CrossEcosystemView()
    with pytest.raises(ValueError, match="exactly one"):
        view.add_chain("X")  # neither
    with pytest.raises(ValueError, match="exactly one"):
        view.add_chain("X", db_path=str(db), export_path=str(db))  # both


def test_view_integrity_without_secrets(tmp_path):
    db = tmp_path / "x.db"
    _make_chain_db(db, [{"provider": "anthropic", "model": "c", "ts": 1}])
    view = CrossEcosystemView()
    view.add_chain("X", db_path=str(db))
    r = view.integrity_report()
    assert r["per_chain"]["X"]["valid"] is True
    assert r["per_chain"]["X"]["method"] == "structural"
    assert "No HMAC secret" in r["per_chain"]["X"]["note"]


def test_view_mixed_db_and_export(tmp_path):
    db = tmp_path / "live.db"
    _make_chain_db(db, [{"provider": "anthropic", "model": "c", "ts": 100}])
    exp = tmp_path / "exp.json"
    exp.write_text(json.dumps({
        "entries": [{
            "timestamp_ns": 200,
            "canonical_body": {
                "attributes": {
                    "gen_ai.provider.name": "openai",
                    "gen_ai.request.model": "d",
                },
            },
        }],
    }))

    view = CrossEcosystemView()
    view.add_chain("DB", db_path=str(db))
    view.add_chain("EXPORT", export_path=str(exp))
    s = view.summary()
    assert s["total_entries"] == 2
    assert s["total_providers"] == ["anthropic", "openai"]


def test_view_cross_chain_overlap_detection(tmp_path):
    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    _make_chain_db(a, [{"provider": "anthropic", "model": "x", "ts": 100},
                       {"provider": "anthropic", "model": "x", "ts": 1000}])
    _make_chain_db(b, [{"provider": "anthropic", "model": "y", "ts": 500}])
    view = CrossEcosystemView()
    view.add_chain("A", db_path=str(a))
    view.add_chain("B", db_path=str(b))
    r = view.integrity_report()
    assert r["cross_chain"]["timeline_overlap"] is True
    assert r["cross_chain"]["shared_providers"] == ["anthropic"]


def test_view_summary_is_json_serializable(tmp_path):
    db = tmp_path / "x.db"
    _make_chain_db(db, [{"provider": "a", "model": "m", "ts": 1}])
    view = CrossEcosystemView()
    view.add_chain("X", db_path=str(db))
    s = view.summary()
    # Round-trip
    j = json.dumps(s)
    back = json.loads(j)
    assert back == s


# ─── CLI tests (subprocess) ─────────────────────────────────────────


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bijotel.cli.main", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_cli_cross_view_json(tmp_path):
    db = tmp_path / "x.db"
    _make_chain_db(db, [
        {"provider": "anthropic", "model": "c", "ts": 100},
    ])
    out = _run_cli("cross-view", "--chain", f"GENA={db}", "--json")
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["ecosystems"] == 1
    assert data["total_entries"] == 1


def test_cli_cross_view_bad_spec(tmp_path):
    out = _run_cli("cross-view", "--chain", "no_equals_sign", "--json")
    assert out.returncode == 2
    assert "NAME=PATH" in out.stderr


def test_cli_cross_view_missing_file(tmp_path):
    out = _run_cli(
        "cross-view", "--chain", f"X={tmp_path}/missing.db", "--json",
    )
    assert out.returncode == 1
    assert "not found" in out.stderr


def test_cli_cross_view_human_table(tmp_path):
    db = tmp_path / "x.db"
    _make_chain_db(db, [{"provider": "a", "model": "m", "ts": 1}])
    out = _run_cli("cross-view", "--chain", f"GENA={db}")
    assert out.returncode == 0
    assert "Ecosystems:" in out.stdout
    assert "GENA" in out.stdout


# ─── public API ─────────────────────────────────────────────────────


def test_public_api_exports():
    import bijotel
    for name in ("CrossEcosystemView", "ChainStats"):
        assert hasattr(bijotel, name), f"bijotel.{name} missing"
        assert name in bijotel.__all__
