"""``bijotel inspect`` / ``stats`` / ``list`` — read-only chain queries.

Three commands that operate on chain.db without mutating it. ``inspect``
prints one row in full canonical detail; ``stats`` rolls up the chain
+ CAS + policy state into one screen; ``list`` is the paginated table
view with filters.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

from bijotel.cli._helpers import (
    _calc_cost,
    _detect_status,
    _format_time,
    _parse_canonical_body,
)
from bijotel.processors import cas_lookup, cas_stats


def _print_rag_sources(body_dict: dict) -> None:
    """Render the ``bijotel.rag.*`` block of a chain entry, if present.

    Skipped silently when the span carries no RAG attributes — which is
    the common case (most LLM calls are not RAG-augmented). When present,
    the sources JSON is parsed and the first ~5 entries shown in a
    compact table; the full record stays visible in the canonical-body
    dump below.
    """
    attrs = body_dict.get("attributes", {})
    src_count = attrs.get("bijotel.rag.source_count")
    if src_count is None:
        return

    retriever = attrs.get("bijotel.rag.retriever_id") or "(none)"
    embedding = attrs.get("bijotel.rag.embedding_model") or "(none)"
    total_tokens = attrs.get("bijotel.rag.total_context_tokens")

    print("\n=== RAG Provenance ===")
    print(f"source_count:   {src_count}")
    print(f"retriever:      {retriever}")
    print(f"embedding:      {embedding}")
    if total_tokens is not None:
        print(f"context_tokens: {total_tokens}")

    sources_raw = attrs.get("bijotel.rag.sources")
    if not sources_raw:
        return
    try:
        sources = json.loads(sources_raw)
    except (json.JSONDecodeError, TypeError):
        print(f"  (sources JSON unparseable: {sources_raw!r})")
        return
    if not isinstance(sources, list):
        return

    # Display first 5; if more, show count of remainder.
    shown = sources[:5]
    for i, s in enumerate(shown, start=1):
        doc = (s.get("document_id") or "")[:36]
        chunk = s.get("chunk_index", "?")
        sim = s.get("similarity_score", 0.0)
        retr = s.get("retriever") or "-"
        print(
            f"  source {i}: doc={doc}  chunk={chunk}  "
            f"retriever={retr}  sim={sim:.3f}"
        )
    if len(sources) > len(shown):
        print(f"  ... and {len(sources) - len(shown)} more")


def inspect_cmd(args: argparse.Namespace) -> int:
    """Inspect span by hex span_id or integer seq."""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 1

    identifier = args.id
    is_seq = identifier.isdigit()

    with sqlite3.connect(db_path) as conn:
        if is_seq:
            row = conn.execute(
                "SELECT * FROM chain WHERE seq = ?", (int(identifier),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM chain WHERE span_id = ?", (identifier,)
            ).fetchone()

        if not row:
            print(f"No span found for: {identifier}", file=sys.stderr)
            return 1

        col_names = [
            d[0]
            for d in conn.execute("SELECT * FROM chain LIMIT 0").description
        ]
        row_dict = dict(zip(col_names, row, strict=False))

    print("=== Span Metadata ===")
    print(f"seq:                 {row_dict['seq']}")
    print(f"timestamp:           {_format_time(row_dict['timestamp_ns'])}")
    print(f"trace_id:            {row_dict['trace_id']}")
    print(f"span_id:             {row_dict['span_id']}")
    print(f"span_name:           {row_dict['span_name']}")
    print(f"span_kind:           {row_dict['span_kind']}")
    print(f"canonical_hash:      {row_dict['canonical_hash']}")
    print(f"semantic_body_hash:  {row_dict.get('semantic_body_hash') or '(none)'}")
    print(f"prev_hash:           {row_dict['prev_hash']}")
    print(f"hmac_hash:           {row_dict['hmac_hash']}")

    body_dict = _parse_canonical_body(row_dict["canonical_body"])
    print(f"\n=== Status: {_detect_status(body_dict)} ===")
    print(f"Cost (real): {_calc_cost(body_dict)}")

    # v2.6.0: surface RAG provenance if present.
    _print_rag_sources(body_dict)

    print("\n=== Canonical Body (decoded) ===")
    print(json.dumps(body_dict, indent=2, ensure_ascii=False))

    # Cross-reference la CAS
    sem_hash = row_dict.get("semantic_body_hash")
    if sem_hash:
        cas_entry = cas_lookup(db_path, sem_hash)
        if cas_entry:
            _, first_seen, ref_count = cas_entry
            print("\n=== CAS Entry ===")
            print(f"body_hash:     {sem_hash}")
            print(f"first_seen:    {_format_time(first_seen)}")
            print(f"ref_count:     {ref_count}")
        else:
            print("\n=== CAS: no entry (no semantic_body_hash) ===")

    return 0


def stats_cmd(args: argparse.Namespace) -> int:
    """Print summary stats: chain, CAS, policy state."""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 1

    with sqlite3.connect(db_path) as conn:
        # Chain stats
        total = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
        rows = conn.execute("SELECT canonical_body FROM chain").fetchall()
        blocked = 0
        warned = 0
        allowed = 0
        for (body,) in rows:
            body_dict = _parse_canonical_body(body)
            status = _detect_status(body_dict)
            if status == "BLOCKED":
                blocked += 1
            elif status == "WARN":
                warned += 1
            else:
                allowed += 1

        print("=== Chain ===")
        print(f"Total spans:    {total}")
        print(f"  ALLOWED:      {allowed}")
        print(f"  WARN:         {warned}")
        print(f"  BLOCKED:      {blocked}")

        # CAS stats
        try:
            cas_data = cas_stats(db_path)
            print("\n=== CAS ===")
            print(f"Unique bodies:  {cas_data['unique_bodies']}")
            print(f"Total refs:     {cas_data['total_refs']}")
            print(f"Dedup factor:   {cas_data['dedup_factor']:.2f}x")
        except sqlite3.OperationalError:
            print("\n=== CAS: table not present ===")

        # Policy daily state
        try:
            policy_rows = conn.execute(
                "SELECT date, total_tokens FROM policy_daily_state "
                "ORDER BY date DESC LIMIT 7"
            ).fetchall()
            if policy_rows:
                print("\n=== Policy Daily State (last 7 days) ===")
                for date, tokens in policy_rows:
                    print(f"  {date}:  {tokens:,} tokens")
        except sqlite3.OperationalError:
            pass  # No policy state table

    return 0


def list_cmd(args: argparse.Namespace) -> int:
    """List spans with optional filters in tabular format."""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 1

    # Build WHERE clause for SQL-supported filters (date)
    conditions: list[str] = []
    params: list = []

    if args.since:
        try:
            since_dt = datetime.datetime.strptime(
                args.since, "%Y-%m-%d"
            ).replace(tzinfo=datetime.UTC)
        except ValueError:
            print("ERROR: --since must be YYYY-MM-DD format", file=sys.stderr)
            return 2
        conditions.append("timestamp_ns >= ?")
        params.append(int(since_dt.timestamp() * 1e9))

    where_sql = ""
    if conditions:
        where_sql = " WHERE " + " AND ".join(conditions)

    sql = (
        "SELECT seq, timestamp_ns, trace_id, span_name, canonical_body "
        f"FROM chain{where_sql} ORDER BY seq DESC LIMIT ? OFFSET ?"
    )
    params.append(args.limit)
    params.append(args.offset)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()

    # Filter post-fetch (necesită parse canonical_body)
    filtered = []
    for seq, ts_ns, trace_id, span_name, body in rows:
        body_dict = _parse_canonical_body(body)
        attrs = body_dict.get("attributes", {})

        if args.blocked and attrs.get("bijotel.blocked") is not True:
            continue

        if args.rule:
            rule_match = (
                attrs.get("bijotel.policy.rule") == args.rule
                or attrs.get("bijotel.policy.warning") == args.rule
            )
            if not rule_match:
                continue

        if args.model and attrs.get("gen_ai.request.model") != args.model:
            continue

        filtered.append((seq, ts_ns, trace_id, span_name, body_dict))

    if not filtered:
        print("(no spans match filters)")
        return 0

    # Tabular output
    print(
        f"{'seq':<6} {'timestamp':<22} {'trace_id':<18} "
        f"{'model':<28} {'status':<8} {'cost':<10}"
    )
    print("-" * 92)
    for seq, ts_ns, trace_id, _span_name, body_dict in filtered:
        attrs = body_dict.get("attributes", {})
        model = (attrs.get("gen_ai.request.model") or "-")[:27]
        status = _detect_status(body_dict)
        cost = _calc_cost(body_dict)
        ts = _format_time(ts_ns)
        trace_short = trace_id[:16] + ".."
        print(
            f"{seq:<6} {ts:<22} {trace_short:<18} "
            f"{model:<28} {status:<8} {cost:<10}"
        )

    print(f"\n({len(filtered)} spans)")
    return 0
