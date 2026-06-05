"""``bijotel energy backfill`` / ``energy summary`` (v1.9.0, F3 / Bijuteria #3).

Two subcommands behind one parser group. ``energy_cmd`` is the dispatch
shim; the two ``_energy_*_cmd`` helpers carry the actual work. v2.4.0
added cache-aware tracker.record() forwarding; the backfill path reads
the v1.41 attributes when present and falls back to 0 for older chain
entries.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def energy_cmd(args: argparse.Namespace) -> int:
    """Dispatch to ``backfill`` or ``summary``.

    Subcommands:
      * ``bijotel energy backfill --db PATH [--region REGION]``
      * ``bijotel energy summary  --db PATH [--since ISO] [--until ISO] [--agent-id ID]``

    Backfill reads each chain.db row, extracts model + token counts
    from the canonical body, and INSERTs into ``energy_log``.
    Idempotent on ``chain.seq`` (UNIQUE index) — re-running it never
    double-counts.

    Exit codes:
        0  — success.
        1  — chain DB missing / unreadable.
        2  — unknown energy subcommand.
    """
    sub = getattr(args, "energy_command", None)
    if sub == "backfill":
        return _energy_backfill_cmd(args)
    if sub == "summary":
        return _energy_summary_cmd(args)
    if sub == "mark-live":
        return _energy_mark_live_cmd(args)
    print(f"ERROR: unknown energy subcommand {sub!r}", file=sys.stderr)
    return 2


def _energy_backfill_cmd(args: argparse.Namespace) -> int:
    from bijotel.layers.energy import CarbonCalculator, EnergyTracker

    db_path = args.db
    if not Path(db_path).is_file():
        print(f"ERROR: chain DB not found at {db_path}", file=sys.stderr)
        return 1

    tracker = EnergyTracker(
        db_path,
        calculator=CarbonCalculator(args.region),
    )

    cutover = tracker.get_live_cutover_seq()
    if cutover is not None:
        print(
            f"Live cutover at seq {cutover}: chain rows with seq > {cutover} are "
            "owned by the live EnergySpanProcessor and will be skipped "
            "(prevents double-counting NULL-seq live rows)."
        )

    inserted = 0
    skipped = 0
    live_skipped = 0
    failed = 0
    with sqlite3.connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
        print(f"Backfilling energy from {total} chain entries...")
        for row in conn.execute(
            "SELECT seq, timestamp_ns, canonical_body FROM chain ORDER BY seq"
        ):
            seq, ts_ns, body = row
            if cutover is not None and seq > cutover:
                live_skipped += 1
                continue
            try:
                d = json.loads(body.decode() if isinstance(body, bytes) else body)
                attrs = d.get("attributes", {})
                model = (
                    attrs.get("gen_ai.request.model")
                    or attrs.get("gen_ai.response.model")
                )
                if not model:
                    skipped += 1
                    continue
                ti = int(attrs.get("gen_ai.usage.input_tokens", 0) or 0)
                to = int(attrs.get("gen_ai.usage.output_tokens", 0) or 0)
                if ti == 0 and to == 0:
                    skipped += 1
                    continue
                # agent_id fallback chain: agent.name → service.name → "default"
                agent = str(
                    attrs.get("agent.name")
                    or attrs.get("service.name")
                    or "default"
                )
                tracker.record(
                    model=str(model),
                    tokens_in=ti,
                    tokens_out=to,
                    timestamp_ns=ts_ns,
                    agent_id=agent,
                    span_seq=seq,
                )
                inserted += 1
            except Exception as e:
                failed += 1
                if failed <= 3:
                    print(
                        f"  WARN seq={seq}: {type(e).__name__}: {e}",
                        file=sys.stderr,
                    )

    print(
        f"Done. inserted={inserted}, skipped={skipped}, "
        f"live_skipped={live_skipped}, failed={failed}, total={total}"
    )

    # Show summary right after
    s = tracker.summary()
    print()
    print(f"Energy log totals (region={args.region}):")
    print(f"  Calls:     {s.total_calls:,}")
    print(f"  Tokens:    {s.total_tokens:,}")
    print(f"  Energy:    {s.total_wh:.3f} Wh  ({s.total_wh/1000:.6f} kWh)")
    print(f"  CO2:       {s.total_co2_grams:.3f} grams  ({s.co2_kg*1000:.0f} mg)")
    print(f"  ≈ km driven (gasoline car): {s.equivalent_km_driven:.4f}")
    print(f"  ≈ phone charges:            {s.equivalent_phone_charges:.4f}")
    print(f"  ≈ kettle boils:             {s.equivalent_kettle_boils:.4f}")
    return 0


def _energy_mark_live_cmd(args: argparse.Namespace) -> int:
    """Set the live-cutover seq so future backfills skip the live-owned tail.

    With ``--seq N`` uses N; otherwise uses the current chain head
    (``MAX(seq)``). Run this ONCE, just before the host starts recording
    energy live via :class:`EnergySpanProcessor`, so backfill (seq <=
    cutover) and live (span_seq NULL, for seq > cutover) cover disjoint
    ranges and never double-count. The marker lives in the DB, so the
    guard holds even if the operator forgets the rule.
    """
    from bijotel.layers.energy import EnergyTracker

    db_path = args.db
    if not Path(db_path).is_file():
        print(f"ERROR: chain DB not found at {db_path}", file=sys.stderr)
        return 1

    seq = args.seq
    if seq is None:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT MAX(seq) FROM chain").fetchone()
        if not row or row[0] is None:
            print("ERROR: chain is empty; nothing to mark.", file=sys.stderr)
            return 1
        seq = int(row[0])

    tracker = EnergyTracker(db_path)
    tracker.set_live_cutover_seq(seq)
    print(
        f"Live cutover set: seq {seq}. Backfill now covers seq <= {seq} and "
        f"skips seq > {seq} (owned by the live EnergySpanProcessor)."
    )
    return 0


def _energy_summary_cmd(args: argparse.Namespace) -> int:
    from bijotel.layers.energy import EnergyTracker

    db_path = args.db
    if not Path(db_path).is_file():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    tracker = EnergyTracker(db_path)
    s = tracker.summary(
        since=args.since,
        until=args.until,
        agent_id=args.agent_id,
    )
    print(f"Energy summary (since={s.since}, until={s.until}):")
    print(f"  Calls:     {s.total_calls:,}")
    print(f"  Tokens:    {s.total_tokens:,}")
    print(f"  Energy:    {s.total_wh:.4f} Wh")
    print(f"  CO2:       {s.total_co2_grams:.4f} grams ({s.co2_kg:.6f} kg)")
    print(f"  ≈ km:      {s.equivalent_km_driven:.4f}")
    print(f"  ≈ charges: {s.equivalent_phone_charges:.4f}")
    if s.per_model:
        print()
        print("Per model:")
        for m in sorted(s.per_model.values(), key=lambda x: -x.wh):
            print(f"  {m.model:40s} {m.calls:6d} calls  {m.wh:8.4f} Wh  {m.co2_grams:7.3f} g")
    if s.per_agent:
        print()
        print("Per agent:")
        for a in sorted(s.per_agent.values(), key=lambda x: -x.wh):
            print(f"  {a.agent_id:20s} {a.calls:6d} calls  {a.wh:8.4f} Wh  {a.co2_grams:7.3f} g")
    return 0
