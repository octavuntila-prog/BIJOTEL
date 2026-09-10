"""Tests for ``bijotel.tools.proof_stats`` (production-proof collector/renderer).

Synthetic ``chain.db`` files are hand-seeded row by row with the same column
set as ``HmacChainSpanProcessor``. The ``hmac_hash`` values are arbitrary
sha256 digests: the tool's link check is keyless (``prev_hash`` must equal
the previous row's ``hmac_hash``, ``canonical_hash`` must equal
``sha256(canonical_body)``), so no HMAC secret is needed here.

Render tests read the saved production snapshots under
``tests/fixtures/proof/`` -- pure text processing, no network.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import bijotel
from bijotel.tools.proof_stats import (
    GENESIS_HASH,
    _ro_connect,
    collect_chain,
    collect_federation,
    main,
    render_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures" / "proof"
NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
NOW_ISO = "2026-09-10T12:00:00Z"

CHAIN_DDL = """
CREATE TABLE chain (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ns INTEGER NOT NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    span_name TEXT NOT NULL,
    span_kind TEXT,
    canonical_body BLOB NOT NULL,
    canonical_hash TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hmac_hash TEXT NOT NULL,
    semantic_body_hash TEXT
)
"""

FEDERATION_DDL = """
CREATE TABLE operators (
    operator_id TEXT PRIMARY KEY, org_name TEXT NOT NULL, public_key_pem TEXT NOT NULL,
    contact_email TEXT NOT NULL DEFAULT '', registered_at TEXT NOT NULL,
    rekor_log_index INTEGER, last_submission_at TEXT
);
CREATE TABLE submissions (
    submission_id TEXT PRIMARY KEY, operator_id TEXT NOT NULL, signed_export_json TEXT NOT NULL,
    entry_count INTEGER NOT NULL, first_seq INTEGER NOT NULL, last_seq INTEGER NOT NULL,
    chain_head_signature TEXT NOT NULL, continuity_verified INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT NOT NULL, cross_anchor_id TEXT
);
CREATE TABLE cross_anchors (
    anchor_id TEXT PRIMARY KEY, cross_anchor_hash TEXT NOT NULL, anchored_at TEXT NOT NULL,
    rekor_log_index INTEGER, rekor_url TEXT, federation_signature TEXT NOT NULL
);
CREATE TABLE anchor_participants (
    anchor_id TEXT NOT NULL, operator_id TEXT NOT NULL, chain_signature TEXT NOT NULL,
    PRIMARY KEY (anchor_id, operator_id)
);
"""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_chain(
    path: Path, n: int = 30, break_at: int | None = None, skip_seq: int | None = None
) -> None:
    """One row per day, the last one 30 min before NOW; prev_hash links to previous hmac."""
    conn = sqlite3.connect(path)
    conn.execute(CHAIN_DDL)
    prev = GENESIS_HASH
    first_ts = NOW - timedelta(minutes=30) - timedelta(days=n - 1)
    for seq in range(1, n + 1):
        ts = first_ts + timedelta(days=seq - 1)
        body = json.dumps({"seq": seq, "name": "chat"}).encode()
        hmac_hash = hashlib.sha256(f"hmac-{seq}".encode()).hexdigest()
        prev_hash = "f" * 64 if seq == break_at else prev
        if seq != skip_seq:
            conn.execute(
                "INSERT INTO chain (seq, timestamp_ns, trace_id, span_id, span_name, span_kind, "
                "canonical_body, canonical_hash, prev_hash, hmac_hash, semantic_body_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    int(ts.timestamp()) * 10**9,
                    f"{seq:032x}",
                    f"{seq:016x}",
                    "chat",
                    "CLIENT",
                    body,
                    hashlib.sha256(body).hexdigest(),
                    prev_hash,
                    hmac_hash,
                    None,
                ),
            )
        prev = hmac_hash
    conn.commit()
    conn.close()


def _build_federation(path: Path, n_anchors: int = 8) -> None:
    """Two operators, ``n_anchors`` daily anchors (one without Rekor index), 2 submissions each."""
    conn = sqlite3.connect(path)
    conn.executescript(FEDERATION_DDL)
    ops = [("op_a", "GENA", "2026-06-05T14:49:30Z"), ("op_b", "ARA", "2026-06-05T14:48:20Z")]
    for oid, org, reg in ops:
        conn.execute(
            "INSERT INTO operators VALUES (?,?,?,?,?,?,?)",
            (oid, org, "-----PEM-----", "", reg, None, "2026-09-10T03:00:03Z"),
        )
    for i in range(1, n_anchors + 1):
        at = _iso(NOW.replace(hour=3, minute=45) - timedelta(days=n_anchors - i))
        aid = f"anchor_{i:03d}"
        idx = None if i == 3 else 1000 + i
        url = None if idx is None else f"https://rekor.sigstore.dev/api/v1/log/entries?logIndex={idx}"
        conn.execute(
            "INSERT INTO cross_anchors VALUES (?,?,?,?,?,?)", (aid, "ab" * 32, at, idx, url, "sig")
        )
        for oid, _org, _reg in ops:
            conn.execute("INSERT INTO anchor_participants VALUES (?,?,?)", (aid, oid, "chainsig"))
            conn.execute(
                "INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"sub_{aid}_{oid}",
                    oid,
                    "{}",
                    10,
                    100 * i,
                    100 * i + 9,
                    "headsig",
                    1,
                    at.replace("03:45", "03:00"),
                    aid,
                ),
            )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# collect: chain
# ---------------------------------------------------------------------


def test_collect_valid_chain_via_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "chain.db"
    _build_chain(db)
    rc = main(["collect", "--chain", str(db), "--label", "T", "--now", NOW_ISO])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["label"] == "T"
    assert out["measured_at_utc"] == NOW_ISO
    assert out["bijotel_version"] == bijotel.__version__
    assert out["federation"] is None

    c = out["chain"]
    assert (c["count"], c["seq_min"], c["seq_max"]) == (30, 1, 30)
    assert c["seq_gaps"] is False
    assert c["first_ts_utc"] == "2026-08-12T11:30:00Z"
    assert c["last_ts_utc"] == "2026-09-10T11:30:00Z"
    assert c["days_running"] == 29
    assert c["head_age_min"] == 30.0
    lc = c["link_valid_last_n"]
    assert lc["valid"] is True
    assert (lc["n_checked"], lc["first_seq"], lc["last_seq"]) == (30, 1, 30)
    assert lc["broken_at"] is None
    hist = c["rows_last_7_days"]
    assert list(hist) == [f"2026-09-{d:02d}" for d in range(4, 11)]
    assert set(hist.values()) == {1}


def test_collect_link_window_only_covers_last_n(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _build_chain(db, break_at=17)
    narrow = collect_chain(db, now=NOW, link_n=5)["link_valid_last_n"]
    # Rows 26..30 are checked against their predecessors; the break at 17 is outside.
    assert narrow["valid"] is True
    assert (narrow["n_checked"], narrow["first_seq"], narrow["last_seq"]) == (5, 26, 30)


def test_collect_broken_link_detected(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _build_chain(db, break_at=17)
    lc = collect_chain(db, now=NOW)["link_valid_last_n"]
    assert lc["valid"] is False
    assert lc["broken_at"] == 17
    assert "prev_hash" in lc["reason"]
    assert lc["n_checked"] == 16


def test_collect_seq_gap_detected(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _build_chain(db, skip_seq=20)
    c = collect_chain(db, now=NOW)
    assert c["count"] == 29
    assert c["seq_gaps"] is True
    assert c["link_valid_last_n"]["valid"] is False
    assert c["link_valid_last_n"]["reason"].startswith("seq gap")


def test_collect_tampered_body_detected(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _build_chain(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE chain SET canonical_body = ? WHERE seq = 25", (b'{"seq": 250}',))
    lc = collect_chain(db, now=NOW)["link_valid_last_n"]
    assert lc["valid"] is False
    assert lc["broken_at"] == 25
    assert "canonical_hash" in lc["reason"]


def test_collect_empty_chain(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    with sqlite3.connect(db) as conn:
        conn.execute(CHAIN_DDL)
    c = collect_chain(db, now=NOW)
    assert c["count"] == 0
    assert c["days_running"] is None
    assert c["link_valid_last_n"] == {
        "valid": None,
        "n_checked": 0,
        "first_seq": None,
        "last_seq": None,
        "broken_at": None,
        "reason": "empty chain",
    }
    # A check that checked nothing must not render as VALID.
    stats = {
        "label": "EMPTY",
        "measured_at_utc": NOW_ISO,
        "bijotel_version": None,
        "chain": c,
        "federation": None,
    }
    text = render_markdown([stats], generated_at=NOW)
    assert "| UNCHECKED (n=0, empty chain) |" in text
    assert "VALID" not in text


def test_collect_window_without_predecessor_is_unchecked(tmp_path: Path) -> None:
    """A one-row chain that does not start at seq 1 has no row to check against."""
    db = tmp_path / "chain.db"
    _build_chain(db, n=7)
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM chain WHERE seq < 7")
    c = collect_chain(db, now=NOW)
    assert (c["count"], c["seq_min"], c["seq_max"]) == (1, 7, 7)
    lc = c["link_valid_last_n"]
    assert lc["valid"] is None
    assert lc["n_checked"] == 0
    assert "predecessor" in lc["reason"]
    stats = {
        "label": "TRIMMED",
        "measured_at_utc": NOW_ISO,
        "bijotel_version": None,
        "chain": c,
        "federation": None,
    }
    text = render_markdown([stats], generated_at=NOW)
    assert "| UNCHECKED (n=0, no row with a known predecessor in the window) |" in text
    assert "VALID" not in text


def test_collect_broken_head_is_broken_not_unchecked(tmp_path: Path) -> None:
    """A break at the very first checked row (n_checked == 0) still renders BROKEN."""
    db = tmp_path / "chain.db"
    _build_chain(db, break_at=30)
    c = collect_chain(db, now=NOW, link_n=1)
    lc = c["link_valid_last_n"]
    assert lc["valid"] is False
    assert (lc["n_checked"], lc["broken_at"]) == (0, 30)
    stats = {
        "label": "HEAD",
        "measured_at_utc": NOW_ISO,
        "bijotel_version": None,
        "chain": c,
        "federation": None,
    }
    text = render_markdown([stats], generated_at=NOW)
    assert "| BROKEN at seq 30 (prev_hash != hmac_hash of previous row, n=0) |" in text
    assert "UNCHECKED" not in text


def test_link_n_below_one_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "chain.db"
    _build_chain(db, n=3, break_at=3)
    for bad in ("0", "-1"):
        with pytest.raises(SystemExit) as exc:
            main(["collect", "--chain", str(db), "--link-n", bad, "--now", NOW_ISO])
        assert exc.value.code == 2
        assert "must be >= 1" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["collect", "--chain", str(db), "--link-n", "x", "--now", NOW_ISO])
    for bad_n in (0, -1):
        with pytest.raises(ValueError, match="link_n must be >= 1"):
            collect_chain(db, now=NOW, link_n=bad_n)
    # The smallest allowed window on that chain finds the broken head.
    lc = collect_chain(db, now=NOW, link_n=1)["link_valid_last_n"]
    assert (lc["valid"], lc["broken_at"]) == (False, 3)


def test_connection_is_read_only(tmp_path: Path) -> None:
    db = tmp_path / "chain.db"
    _build_chain(db, n=2)
    conn = _ro_connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM chain")
    finally:
        conn.close()


def test_collect_requires_a_source() -> None:
    with pytest.raises(SystemExit):
        main(["collect"])


# ---------------------------------------------------------------------
# collect: federation
# ---------------------------------------------------------------------


def test_collect_federation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "federation.db"
    _build_federation(db)
    f = collect_federation(db)

    assert f["operators"]["count"] == 2
    assert f["operators"]["ids"] == ["op_b", "op_a"]  # ordered by registered_at
    assert f["operators"]["items"][0]["last_submission_at"] == "2026-09-10T03:00:03Z"
    assert f["operators"]["items"][0]["submissions"] == 8
    assert f["anchors_total"] == 8
    assert f["rekor_anchored"] == 7
    assert f["first_anchor_at"] == "2026-09-03T03:45:00Z"
    assert f["last_anchor"]["anchor_id"] == "anchor_008"
    assert f["last_anchor"]["anchored_at"] == "2026-09-10T03:45:00Z"
    assert f["last_anchor"]["rekor_log_index"] == 1008
    assert f["last_anchor"]["rekor_url"].endswith("logIndex=1008")
    assert f["last_anchor"]["participants"] == 2
    assert [a["anchor_id"] for a in f["last_7_anchors"]] == [
        f"anchor_{i:03d}" for i in range(8, 1, -1)
    ]
    assert f["last_7_anchors"][5]["rekor_log_index"] is None  # anchor_003
    assert f["submissions_total"] == 16
    assert f["last_submission"]["submitted_at"] == "2026-09-10T03:00:00Z"
    assert f["last_submission"]["cross_anchor_id"] == "anchor_008"

    # federation-only run (the ARA host case): chain is null, JSON still complete.
    rc = main(["collect", "--federation", str(db), "--label", "FED", "--now", NOW_ISO])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["chain"] is None
    assert out["federation"]["anchors_total"] == 8


# ---------------------------------------------------------------------
# render
# ---------------------------------------------------------------------


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_saved_fixtures_have_collect_shape() -> None:
    for name in ("gena.json", "ara.json", "federation.json"):
        s = _load(name)
        assert set(s) == {"label", "measured_at_utc", "bijotel_version", "chain", "federation"}
        assert s["measured_at_utc"].endswith("Z")
    for name in ("gena.json", "ara.json"):
        c = _load(name)["chain"]
        assert c["link_valid_last_n"]["valid"] is True
        assert c["link_valid_last_n"]["n_checked"] == c["link_check_n"]
        assert c["seq_gaps"] is False
    fed = _load("federation.json")
    assert fed["label"] == "FEDERATION"
    assert fed["chain"] is None
    assert fed["bijotel_version"] is None  # ARA host has no bijotel package
    assert fed["federation"]["rekor_anchored"] <= fed["federation"]["anchors_total"]
    assert len(fed["federation"]["last_7_anchors"]) == 7


def test_render_from_saved_fixtures(tmp_path: Path) -> None:
    out = tmp_path / "PROOF.md"
    inputs = [str(FIXTURES / n) for n in ("gena.json", "ara.json", "federation.json")]
    assert main(["render", *inputs, "-o", str(out)]) == 0
    text = out.read_bytes().decode("utf-8")
    assert "\r" not in text

    gena, ara, fed = _load("gena.json"), _load("ara.json"), _load("federation.json")
    assert text.startswith("# BIJOTEL production proof\n\nGenerated 20")
    assert "(UTC)." in text.splitlines()[2]
    assert "## Chains" in text
    gena_since, gena_days = gena["chain"]["first_ts_utc"][:10], gena["chain"]["days_running"]
    assert f"| GENA | {gena_since} | {gena_days} |" in text
    assert f"| ARA | {ara['chain']['first_ts_utc'][:10]} |" in text
    assert f"{gena['chain']['count']:,}" in text
    assert f"VALID (n={gena['chain']['link_valid_last_n']['n_checked']}" in text
    assert gena["measured_at_utc"] in text
    assert gena["bijotel_version"] in text

    last = fed["federation"]["last_anchor"]
    assert "## Federation" in text
    assert f"- Operators: **{fed['federation']['operators']['count']}**" in text
    assert f"**{fed['federation']['anchors_total']:,}** total" in text
    assert f"`{last['anchor_id']}` at {last['anchored_at']}" in text
    assert f"[{last['rekor_log_index']}]({last['rekor_url']})" in text
    assert "### Last 7 anchors" in text
    assert text.count("https://rekor.sigstore.dev/api/v1/log/entries?logIndex=") >= 7

    assert "## What this proves / what it does not" in text
    assert "constitute certification" in text
    assert "does **not** by itself detect a rewritten history" in text
    assert "operator-side manual check" in text
    footer = text.rstrip().splitlines()[-1]
    assert footer.startswith("Generated by hand at 20")
    assert footer.endswith("-- not by the scheduled job described in docs/ops/proof-page.md.")


def test_render_degrades_without_sources() -> None:
    gena = _load("gena.json")
    only_chain = render_markdown([gena], generated_at=NOW)
    assert "Generated 2026-09-10T12:00:00Z (UTC)." in only_chain
    assert "| GENA |" in only_chain
    assert "No federation statistics were supplied." in only_chain

    nothing = render_markdown([], generated_at=NOW)
    assert "No chain statistics were supplied." in nothing
    assert "No federation statistics were supplied." in nothing
    assert nothing.rstrip().endswith(
        "Generated by hand at 2026-09-10T12:00:00Z with "
        "`python -m bijotel.tools.proof_stats render` "
        "-- not by the scheduled job described in docs/ops/proof-page.md."
    )


def test_render_footer_says_how_the_page_was_produced(tmp_path: Path) -> None:
    gena = _load("gena.json")
    manual = render_markdown([gena], generated_at=NOW)
    assert manual.rstrip().endswith("not by the scheduled job described in docs/ops/proof-page.md.")
    assert "Regenerated automatically" not in manual

    cron = render_markdown([gena], generated_at=NOW, generated_by="cron")
    assert cron.rstrip().endswith(
        "Regenerated automatically at 2026-09-10T12:00:00Z by the scheduled job described "
        "in docs/ops/proof-page.md."
    )
    assert "Generated by hand" not in cron

    with pytest.raises(ValueError, match="generated_by"):
        render_markdown([gena], generated_at=NOW, generated_by="robot")

    out = tmp_path / "PROOF.md"
    src = str(FIXTURES / "gena.json")
    assert main(["render", src, "-o", str(out)]) == 0
    assert "Generated by hand at 20" in out.read_text(encoding="utf-8").splitlines()[-1]
    assert main(["render", src, "-o", str(out), "--generated-by", "cron"]) == 0
    assert "Regenerated automatically at 20" in out.read_text(encoding="utf-8").splitlines()[-1]
    with pytest.raises(SystemExit):
        main(["render", src, "-o", str(out), "--generated-by", "robot"])
