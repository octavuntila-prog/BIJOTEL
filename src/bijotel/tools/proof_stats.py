"""Production-proof statistics: read-only collector + Markdown renderer.

``python -m bijotel.tools.proof_stats collect --chain chain.db
[--federation federation.db] [--label GENA]`` prints ONE JSON object to
stdout describing a chain (count, seq range, first/last timestamps, days
running, head age, a ``prev_hash == previous hmac_hash`` link check over the
last N rows, seq gaps, rows per UTC day for the last 7 days) and, optionally,
a federation DB (operators, cross-anchors, Rekor anchoring, last anchor,
last 7 anchors).

``python -m bijotel.tools.proof_stats render a.json b.json -o PROOF.md``
turns one or more collected JSON files into a Markdown page.

Design constraints:

* stdlib only -- ``bijotel`` is imported lazily, only for its version, and
  its absence degrades to ``"bijotel_version": null``. This lets the module
  source be piped into a bare ``python3 -`` on a host without the package;
* every SQLite open uses ``?mode=ro`` -- the tool never writes to a chain;
* no network -- the Rekor URL is reported exactly as stored in the
  federation DB, it is not fetched;
* fast on a ~75k-row chain -- the link check reads only the last N+1 rows
  through the ``seq`` primary key; the per-day histogram scans a single
  integer column.

The link check is keyless: it verifies ``prev_hash(seq) == hmac_hash(seq-1)``
(genesis ``prev_hash`` is 64 zeros) and ``canonical_hash == sha256(body)``.
It does NOT recompute the HMAC (that needs the operator secret) -- see
``bijotel verify`` for the keyed check. Its result is three-state: valid,
broken, or ``null`` when nothing could be checked (empty chain, or a window
holding no row with a known predecessor) -- the page renders that third case
as ``UNCHECKED``, never as a pass. ``--link-n`` must be >= 1.

``render`` states in the footer how the page was produced: by hand
(default) or by the scheduled job of ``docs/ops/proof-page.md``
(``--generated-by cron``), which is not deployed as of 2026-09-10.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64
DEFAULT_LINK_N = 1000
DEFAULT_HISTOGRAM_DAYS = 7
DEFAULT_LAST_ANCHORS = 7

# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> datetime:
    """Parse ``YYYY-MM-DDTHH:MM:SSZ`` (or an offset form) into an aware UTC dt."""
    cleaned = text.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _ns_to_dt(ns: int) -> datetime:
    return datetime.fromtimestamp(ns // 1_000_000_000, tz=UTC)


def _ro_connect(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only (``?mode=ro`` URI)."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _bijotel_version() -> str | None:
    try:
        import bijotel  # lazy on purpose: the host running this may lack it
    except Exception:  # pragma: no cover - exercised on hosts without bijotel
        return None
    return getattr(bijotel, "__version__", None)


def _as_bytes(body: Any) -> bytes:
    if isinstance(body, bytes):
        return body
    if isinstance(body, memoryview):
        return body.tobytes()
    return str(body).encode("utf-8")


# ---------------------------------------------------------------------
# chain
# ---------------------------------------------------------------------


def _link_check(conn: sqlite3.Connection, link_n: int) -> dict[str, Any]:
    """Verify prev_hash/hmac_hash linkage and canonical_hash over the last N rows.

    Fetches the last ``link_n + 1`` rows (the extra one is the predecessor
    that seeds the expected ``prev_hash``). ``n_checked`` counts rows whose
    ``prev_hash`` was compared against a known predecessor (or genesis).

    ``valid`` is a three-state result: ``True`` (every checked row linked),
    ``False`` (a break was found), ``None`` (nothing could be checked -- the
    chain is empty, or the window holds no row with a known predecessor). A
    check that checked nothing is never reported as valid; ``reason`` says why.
    """
    if link_n < 1:
        raise ValueError("link_n must be >= 1")
    rows = conn.execute(
        "SELECT seq, prev_hash, hmac_hash, canonical_body, canonical_hash "
        "FROM chain ORDER BY seq DESC LIMIT ?",
        (link_n + 1,),
    ).fetchall()
    rows.reverse()
    result: dict[str, Any] = {
        "valid": True,
        "n_checked": 0,
        "first_seq": None,
        "last_seq": None,
        "broken_at": None,
        "reason": None,
    }
    if not rows:
        result.update(valid=None, reason="empty chain")
        return result

    expected: str | None = GENESIS_HASH if rows[0][0] == 1 else None
    prev_seq: int | None = None
    for seq, prev_hash, hmac_hash, body, canonical_hash in rows:
        if expected is None:
            # Predecessor row: only seeds the expected prev_hash, not counted.
            expected, prev_seq = hmac_hash, seq
            continue
        if prev_seq is not None and seq != prev_seq + 1:
            result.update(
                valid=False, broken_at=seq, reason=f"seq gap between {prev_seq} and {seq}"
            )
            return result
        if prev_hash != expected:
            result.update(
                valid=False, broken_at=seq, reason="prev_hash != hmac_hash of previous row"
            )
            return result
        if hashlib.sha256(_as_bytes(body)).hexdigest() != canonical_hash:
            result.update(
                valid=False, broken_at=seq, reason="canonical_hash != sha256(canonical_body)"
            )
            return result
        result["n_checked"] += 1
        if result["first_seq"] is None:
            result["first_seq"] = seq
        result["last_seq"] = seq
        expected, prev_seq = hmac_hash, seq
    if result["n_checked"] == 0:
        # Only the seeding predecessor was in the window: nothing was compared.
        result.update(valid=None, reason="no row with a known predecessor in the window")
    return result


def _rows_per_day(conn: sqlite3.Connection, now: datetime, days: int) -> dict[str, int]:
    """Entries per UTC calendar day for the ``days`` days ending today (zeros kept)."""
    start_date = now.date() - timedelta(days=days - 1)
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC)
    since_ns = int(start_dt.timestamp()) * 1_000_000_000
    counts = dict(
        conn.execute(
            "SELECT date(timestamp_ns / 1000000000, 'unixepoch') AS d, COUNT(*) "
            "FROM chain WHERE timestamp_ns >= ? GROUP BY d",
            (since_ns,),
        ).fetchall()
    )
    out: dict[str, int] = {}
    for i in range(days):
        day = (start_date + timedelta(days=i)).isoformat()
        out[day] = int(counts.get(day, 0))
    return out


def collect_chain(
    path: str | Path,
    now: datetime | None = None,
    link_n: int = DEFAULT_LINK_N,
    histogram_days: int = DEFAULT_HISTOGRAM_DAYS,
) -> dict[str, Any]:
    """Read-only statistics for one ``chain`` table."""
    if link_n < 1:
        raise ValueError(f"link_n must be >= 1 (got {link_n})")
    now = now or _utc_now()
    conn = _ro_connect(path)
    try:
        count, seq_min, seq_max, ts_min, ts_max = conn.execute(
            "SELECT COUNT(*), MIN(seq), MAX(seq), MIN(timestamp_ns), MAX(timestamp_ns) FROM chain"
        ).fetchone()
        stats: dict[str, Any] = {
            "path": str(path),
            "count": int(count),
            "seq_min": seq_min,
            "seq_max": seq_max,
            "first_ts_utc": None,
            "last_ts_utc": None,
            "days_running": None,
            "head_age_min": None,
            "seq_gaps": False,
            "link_check_n": link_n,
            "link_valid_last_n": _link_check(conn, link_n),
            "rows_last_7_days": _rows_per_day(conn, now, histogram_days),
        }
        if count:
            first_dt, last_dt = _ns_to_dt(ts_min), _ns_to_dt(ts_max)
            stats["first_ts_utc"] = _iso(first_dt)
            stats["last_ts_utc"] = _iso(last_dt)
            stats["days_running"] = (now.date() - first_dt.date()).days
            stats["head_age_min"] = round((now - last_dt).total_seconds() / 60, 1)
            stats["seq_gaps"] = (seq_max - seq_min + 1) != count
        return stats
    finally:
        conn.close()


# ---------------------------------------------------------------------
# federation
# ---------------------------------------------------------------------

_ANCHOR_SQL = (
    "SELECT a.anchor_id, a.anchored_at, a.rekor_log_index, a.rekor_url, "
    "(SELECT COUNT(*) FROM anchor_participants p WHERE p.anchor_id = a.anchor_id) "
    "FROM cross_anchors a ORDER BY a.anchored_at DESC LIMIT ?"
)

_SUBMISSION_COLS = (
    "submission_id",
    "operator_id",
    "entry_count",
    "first_seq",
    "last_seq",
    "submitted_at",
    "cross_anchor_id",
)


def collect_federation(path: str | Path, last_n: int = DEFAULT_LAST_ANCHORS) -> dict[str, Any]:
    """Read-only statistics for a federation DB (operators/submissions/cross_anchors)."""
    conn = _ro_connect(path)
    try:
        operators = [
            {
                "operator_id": oid,
                "org_name": org,
                "registered_at": reg,
                "last_submission_at": last,
                "submissions": int(n),
            }
            for oid, org, reg, last, n in conn.execute(
                "SELECT o.operator_id, o.org_name, o.registered_at, o.last_submission_at, "
                "(SELECT COUNT(*) FROM submissions s WHERE s.operator_id = o.operator_id) "
                "FROM operators o ORDER BY o.registered_at"
            )
        ]
        total, rekor, first_at = conn.execute(
            "SELECT COUNT(*), COUNT(rekor_log_index), MIN(anchored_at) FROM cross_anchors"
        ).fetchone()
        anchors = [
            {
                "anchor_id": aid,
                "anchored_at": at,
                "rekor_log_index": idx,
                "rekor_url": url,
                "participants": int(n),
            }
            for aid, at, idx, url, n in conn.execute(_ANCHOR_SQL, (last_n,))
        ]
        (subs_total,) = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()
        last_sub = conn.execute(
            "SELECT submission_id, operator_id, entry_count, first_seq, last_seq, "
            "submitted_at, cross_anchor_id FROM submissions ORDER BY submitted_at DESC LIMIT 1"
        ).fetchone()
        return {
            "path": str(path),
            "operators": {
                "count": len(operators),
                "ids": [o["operator_id"] for o in operators],
                "items": operators,
            },
            "submissions_total": int(subs_total),
            "last_submission": (
                dict(zip(_SUBMISSION_COLS, last_sub, strict=True)) if last_sub else None
            ),
            "anchors_total": int(total),
            "rekor_anchored": int(rekor),
            "first_anchor_at": first_at,
            "last_anchor": anchors[0] if anchors else None,
            "last_7_anchors": anchors,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------
# collect (JSON)
# ---------------------------------------------------------------------


def collect(
    chain: str | Path | None,
    federation: str | Path | None,
    label: str,
    now: datetime | None = None,
    link_n: int = DEFAULT_LINK_N,
) -> dict[str, Any]:
    now = now or _utc_now()
    return {
        "label": label,
        "measured_at_utc": _iso(now),
        "bijotel_version": _bijotel_version(),
        "chain": collect_chain(chain, now=now, link_n=link_n) if chain else None,
        "federation": collect_federation(federation) if federation else None,
    }


# ---------------------------------------------------------------------
# render (Markdown)
# ---------------------------------------------------------------------

WHAT_THIS_PROVES = """\
## What this proves / what it does not

**It proves** that the chains exist, grow over time, self-link (the
`prev_hash` of each entry equals the `hmac_hash` of the previous entry,
checked over the last N rows above without the operator HMAC key), and that
their heads are witnessed daily in a public transparency log (Sigstore Rekor)
through the federation cross-anchor -- follow any Rekor link above to check
for yourself.

**It does not prove** that the content of any entry is correct, complete, or
was produced by the claimed model; it does **not** constitute certification,
compliance, or conformity with any regulation; and the federation witness
does **not** by itself detect a rewritten history (an operator could rebuild
its chain and submit a new head -- only comparison against earlier witnessed
heads reveals this).

Continuity check status: **operator-side manual check** -- consistency of a
submitted head against the live chain is verified by the operators by hand
until the federation continuity check is deployed. The `continuity_verified`
flag stored by the federation service is not evidence of that check.
"""

GENERATED_BY = ("manual", "cron")


def _footer(generated_at: datetime, generated_by: str) -> str:
    """State how this particular page was produced -- by hand or by the job.

    The scheduled job of ``docs/ops/proof-page.md`` is not deployed as of
    2026-09-10, so the default must not claim automation.
    """
    if generated_by not in GENERATED_BY:
        raise ValueError(f"generated_by must be one of {GENERATED_BY} (got {generated_by!r})")
    if generated_by == "cron":
        return (
            f"Regenerated automatically at {_iso(generated_at)} by the scheduled job "
            "described in docs/ops/proof-page.md."
        )
    return (
        f"Generated by hand at {_iso(generated_at)} with "
        "`python -m bijotel.tools.proof_stats render` "
        "-- not by the scheduled job described in docs/ops/proof-page.md."
    )


def _fmt_int(n: Any) -> str:
    return f"{int(n):,}" if n is not None else "-"


def _rekor_cell(idx: Any, url: Any) -> str:
    if url and idx is not None:
        return f"[{idx}]({url})"
    return str(idx) if idx is not None else "none"


def _link_cell(chain: dict[str, Any]) -> str:
    lc = chain.get("link_valid_last_n") or {}
    n = lc.get("n_checked", 0)
    valid = lc.get("valid")
    if valid is None:
        # Nothing was compared -- never render that as a pass.
        return f"UNCHECKED (n={n}, {lc.get('reason')})"
    if valid:
        window = (
            f", seq {lc['first_seq']}..{lc['last_seq']}" if lc.get("first_seq") is not None else ""
        )
        return f"VALID (n={n}{window})"
    return f"BROKEN at seq {lc.get('broken_at')} ({lc.get('reason')}, n={n})"


def _render_chain_table(chains: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Chains",
        "",
        "| Chain | Running since | Days | Entries (seq range) | Head age at measurement "
        "| Link check (keyless) | bijotel | Measured (UTC) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in chains:
        c = s["chain"]
        since = (c.get("first_ts_utc") or "-")[:10]
        gaps = "gaps!" if c.get("seq_gaps") else "no gaps"
        entries = f"{_fmt_int(c.get('count'))} (seq {c.get('seq_min')}..{c.get('seq_max')}, {gaps})"
        head = f"{c['head_age_min']} min" if c.get("head_age_min") is not None else "-"
        lines.append(
            f"| {s['label']} | {since} | {c.get('days_running', '-')} | {entries} | {head} "
            f"| {_link_cell(c)} | {s.get('bijotel_version') or 'n/a'} | {s['measured_at_utc']} |"
        )
    lines.append("")
    lines.append("Entries per UTC day, last 7 days ending on the measurement day:")
    lines.append("")
    for s in chains:
        hist = s["chain"].get("rows_last_7_days") or {}
        parts = " / ".join(f"{d}: {n}" for d, n in hist.items())
        lines.append(f"- **{s['label']}** -- {parts}")
    lines.append("")
    return lines


def _render_federation(stats: dict[str, Any]) -> list[str]:
    f = stats["federation"]
    measured = stats["measured_at_utc"]
    ops = f.get("operators") or {}
    lines = ["## Federation", "", f"Measured {measured} from `{f.get('path', '?')}`.", ""]
    lines.append(f"- Operators: **{ops.get('count', 0)}**")
    for o in ops.get("items", []):
        lines.append(
            f"  - `{o['operator_id']}` ({o.get('org_name') or '?'}) -- registered "
            f"{o.get('registered_at')}, last submission {o.get('last_submission_at') or 'never'}, "
            f"{o.get('submissions', 0)} submissions"
        )
    lines.append(
        f"- Cross-anchors: **{_fmt_int(f.get('anchors_total'))}** total, "
        f"**{_fmt_int(f.get('rekor_anchored'))}** with a Rekor log index"
    )
    lines.append(f"- First anchor: {f.get('first_anchor_at') or '-'}")
    last = f.get("last_anchor")
    if last:
        lines.append(
            f"- Last anchor: `{last['anchor_id']}` at {last['anchored_at']} -- "
            f"{last.get('participants', '?')} participants, Rekor log index "
            f"{_rekor_cell(last.get('rekor_log_index'), last.get('rekor_url'))}"
        )
    else:
        lines.append("- Last anchor: none")
    sub = f.get("last_submission")
    if sub:
        lines.append(
            f"- Last submission: `{sub['submission_id']}` by `{sub['operator_id']}` at "
            f"{sub['submitted_at']} -- {sub['entry_count']} entries, seq "
            f"{sub['first_seq']}..{sub['last_seq']} "
            f"(each operator submits the last entries of its chain once a day; the "
            f"entries between two daily submissions are not individually witnessed)"
        )
    lines.append("")
    lines.append("### Last 7 anchors")
    lines.append("")
    lines.append("| Anchor | Anchored at (UTC) | Participants | Rekor log index |")
    lines.append("|---|---|---|---|")
    for a in f.get("last_7_anchors", []):
        cell = _rekor_cell(a.get("rekor_log_index"), a.get("rekor_url"))
        lines.append(
            f"| `{a['anchor_id']}` | {a['anchored_at']} | {a.get('participants', '?')} | {cell} |"
        )
    lines.append("")
    return lines


def render_markdown(
    stats_list: list[dict[str, Any]],
    generated_at: datetime | None = None,
    generated_by: str = "manual",
) -> str:
    generated_at = generated_at or _utc_now()
    footer = _footer(generated_at, generated_by)
    chains = [s for s in stats_list if s.get("chain")]
    feds = [s for s in stats_list if s.get("federation")]
    lines = [
        "# BIJOTEL production proof",
        "",
        f"Generated {_iso(generated_at)} (UTC).",
        "",
        "Every number on this page carries the UTC time it was measured; the page is a "
        "snapshot, not a live view. All measurements are read-only (`?mode=ro`) and keyless "
        "-- see the last section for what this does and does not show.",
        "",
    ]
    if chains:
        lines.extend(_render_chain_table(chains))
    else:
        lines.extend(["## Chains", "", "No chain statistics were supplied.", ""])
    if feds:
        for s in feds:
            lines.extend(_render_federation(s))
    else:
        lines.extend(["## Federation", "", "No federation statistics were supplied.", ""])
    lines.append(WHAT_THIS_PROVES)
    lines.append("---")
    lines.append("")
    lines.append(footer)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def _positive_int(text: str) -> int:
    """argparse type for ``--link-n``: an int >= 1 (0 or less checks nothing)."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1 (got {value})")
    return value


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bijotel.tools.proof_stats",
        description="Read-only production-proof statistics (collect -> JSON, render -> Markdown).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="print one JSON object with chain/federation statistics")
    c.add_argument("--chain", help="path to a bijotel chain SQLite DB (opened read-only)")
    c.add_argument("--federation", help="path to a federation SQLite DB (opened read-only)")
    c.add_argument("--label", default="chain", help="label for this source (e.g. GENA)")
    c.add_argument(
        "--link-n",
        type=_positive_int,
        default=DEFAULT_LINK_N,
        help=f"rows to link-check from the head, >= 1 (default {DEFAULT_LINK_N})",
    )
    c.add_argument("--now", help="override the measurement time (ISO-8601 UTC); for tests")

    r = sub.add_parser("render", help="render collected JSON files into a Markdown page")
    r.add_argument("inputs", nargs="+", help="JSON files produced by 'collect'")
    r.add_argument("-o", "--output", required=True, help="Markdown file to write")
    r.add_argument(
        "--generated-by",
        choices=GENERATED_BY,
        default="manual",
        help="how this page is being produced; sets the footer (default: manual)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "collect":
        if not args.chain and not args.federation:
            parser.error("at least one of --chain / --federation is required")
        now = _parse_iso(args.now) if args.now else None
        stats = collect(args.chain, args.federation, args.label, now=now, link_n=args.link_n)
        sys.stdout.write(json.dumps(stats, indent=2) + "\n")
        return 0

    stats_list = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.inputs]
    text = render_markdown(stats_list, generated_by=args.generated_by)
    Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    sys.stderr.write(f"wrote {args.output} ({len(stats_list)} source(s))\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
