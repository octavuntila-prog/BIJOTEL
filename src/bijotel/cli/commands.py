"""CLI subcommand handlers."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path

from bijotel.policy.prices import DEFAULT_PRICES
from bijotel.processors import (
    cas_lookup,
    cas_stats,
    export_chain,
    inspect_export,
    verify_chain,
    verify_export,
)
from bijotel.regression import Anomaly, RegressionDetector

# ───────────────────────── Helpers ─────────────────────────


def _resolve_secret(args: argparse.Namespace) -> bytes | None:
    """Get HMAC secret from --secret-hex flag or BIJOTEL_HMAC_SECRET env var.

    Returns:
        bytes if available, None otherwise (caller decides if required).
    """
    hex_str = args.secret_hex or os.environ.get("BIJOTEL_HMAC_SECRET")
    if not hex_str:
        return None
    try:
        return bytes.fromhex(hex_str)
    except ValueError:
        print(f"ERROR: Invalid hex in secret: {hex_str[:8]}...", file=sys.stderr)
        sys.exit(2)


def _parse_canonical_body(body: bytes) -> dict:
    """Parse canonical_body BLOB to dict."""
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _detect_status(body_dict: dict) -> str:
    """Return BLOCKED / WARN / ALLOWED based on attributes."""
    attrs = body_dict.get("attributes", {})
    if attrs.get("bijotel.blocked") is True:
        return "BLOCKED"
    if "bijotel.policy.warning" in attrs:
        return "WARN"
    return "ALLOWED"


def _calc_cost(body_dict: dict) -> str:
    """Calculate real cost from gen_ai.usage attrs.

    Returns:
        - "-"            if usage tokens missing (cannot compute)
        - "?<model>"     if model not in DEFAULT_PRICES (unknown pricing).
                         Includes model name fragment so user knows WHICH model
                         is missing — actionable feedback vs opaque "?".
        - "$0.0000"      ONLY when both input and output tokens are 0
                         (e.g. blocked span with no real call)
        - "<$0.0001"     when computed cost > 0 but < $0.0001
                         (preserves signal: real call, just tiny — vs zero)
        - "$0.NNNN"      4-decimal format for normal costs

    Fixes v0.2.0 inconsistency where:
        - Sonnet 4 (claude-sonnet-4-20250514) returned "?" because not in
          price table (now added in prices.py).
        - Tiny Haiku calls returned "$0.0000" (indistinguishable from
          truly-zero blocked spans). Now distinguished as "<$0.0001".
    """
    attrs = body_dict.get("attributes", {})
    model = attrs.get("gen_ai.request.model", "")
    input_tokens = attrs.get("gen_ai.usage.input_tokens")
    output_tokens = attrs.get("gen_ai.usage.output_tokens")

    if input_tokens is None or output_tokens is None:
        return "-"
    if model not in DEFAULT_PRICES:
        # Include short model fragment for actionable feedback
        model_short = model[:30] if model else "(none)"
        return f"?{model_short}"

    cost = (
        input_tokens * DEFAULT_PRICES[model]["input"]
        + output_tokens * DEFAULT_PRICES[model]["output"]
    ) / 1000

    if cost == 0:
        return "$0.0000"
    if cost < 0.0001:
        # Real call, but rounds to zero at 4 decimals — distinguish signal
        return "<$0.0001"
    return f"${cost:.4f}"


def _format_time(timestamp_ns: int) -> str:
    """Convert ns timestamp to ISO8601 UTC string."""
    seconds = timestamp_ns / 1e9
    dt = datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ───────────────────────── verify ─────────────────────────


def verify_cmd(args: argparse.Namespace) -> int:
    """Verify chain integrity."""
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 1

    secret = _resolve_secret(args)
    if secret is None:
        print(
            "ERROR: HMAC secret required. Provide via BIJOTEL_HMAC_SECRET env "
            "var or --secret-hex flag.",
            file=sys.stderr,
        )
        return 2

    valid, seq, reason = verify_chain(db_path, secret)
    with sqlite3.connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]

    if valid:
        print(f"Chain VALID ({total} entries).")
        return 0
    print(f"Chain BROKEN at seq={seq}: {reason}", file=sys.stderr)
    return 3


# ───────────────────────── inspect ─────────────────────────


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


# ───────────────────────── stats ─────────────────────────


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


# ───────────────────────── list ─────────────────────────


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


# ───────────────────── bijotel export ─────────────────────


def export_cmd(args: argparse.Namespace) -> int:
    """Export chain.db to portable signed JSON file (F8).

    With ``--sign-key PATH``, additionally embeds an Ed25519 signature so
    auditors can verify the export without holding the HMAC secret
    (v2.1.0+). Without the flag, the export is the same v1 format
    BIJOTEL has shipped since v1.1.
    """
    secret = _resolve_secret(args)
    if secret is None:
        print(
            "ERROR: HMAC secret required (--secret-hex or BIJOTEL_HMAC_SECRET).",
            file=sys.stderr,
        )
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: chain DB not found: {db_path}", file=sys.stderr)
        return 2

    sign_key_path = getattr(args, "sign_key", None)
    if sign_key_path and not Path(sign_key_path).exists():
        print(f"ERROR: --sign-key path not found: {sign_key_path}", file=sys.stderr)
        return 2

    try:
        out = export_chain(db_path, args.output, secret, sign_key_path=sign_key_path)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: export failed: {e}", file=sys.stderr)
        return 2

    print(f"Exported chain to {out}")
    info = inspect_export(out)
    fmt = info.get("format", "?")
    print(f"  format:        {fmt}")
    print(f"  entries_count: {info.get('entries_count', '?')}")
    head_hash = info.get("head_hash") or "?"
    print(f"  head_hash:     {head_hash[:16]}...")
    print(f"  size:          {info.get('size_bytes', 0):,} bytes")
    if info.get("signed"):
        fp = info.get("public_key_fingerprint") or "?"
        print(f"  signed by:     {fp} (Ed25519)")
    return 0


# ───────────────────── bijotel keygen ─────────────────────


def keygen_cmd(args: argparse.Namespace) -> int:
    """Generate an Ed25519 keypair for signing chain exports.

    Writes two PEM files into ``--output-dir`` (default ``./keys``):

      * ``bijotel_private.pem`` — keep secret. Used by ``bijotel export
        --sign-key``.
      * ``bijotel_public.pem``  — distribute to auditors. Used by
        ``bijotel verify-export --public-key``.

    Refuses to overwrite an existing private key file unless ``--force``
    is passed; rotating a signing key by accident is bad.
    """
    from bijotel.crypto.ed25519 import generate_keypair, public_key_fingerprint

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    priv_path = out_dir / "bijotel_private.pem"
    pub_path = out_dir / "bijotel_public.pem"

    if priv_path.exists() and not args.force:
        print(
            f"ERROR: {priv_path} already exists. Pass --force to overwrite "
            "(this rotates your signing key — old signed exports will no "
            "longer carry a matching key).",
            file=sys.stderr,
        )
        return 2

    private_pem, public_pem = generate_keypair()
    priv_path.write_bytes(private_pem)
    pub_path.write_bytes(public_pem)
    # Lock down private key permissions where the OS supports it.
    # On Windows / restricted FS the chmod is a no-op and we don't
    # treat it as fatal — the private key is still inside the user's
    # directory and the documented expectation is they protect it via
    # secrets manager in production.
    import contextlib
    with contextlib.suppress(OSError):
        os.chmod(priv_path, 0o600)

    fp = public_key_fingerprint(public_pem)
    print(f"Generated Ed25519 keypair in {out_dir}/")
    print(f"  Private key: {priv_path}  (mode 0o600 — keep secret)")
    print(f"  Public key:  {pub_path}   (share with auditors)")
    print(f"  Fingerprint: {fp}")
    print()
    print("Next steps:")
    print(f"  bijotel export --db chain.db -o export.json --sign-key {priv_path}")
    print(
        f"  bijotel verify-export export.json --public-key {pub_path} "
        "[--secret-hex <hex>]"
    )
    return 0


# ─────────────────── bijotel verify-export ────────────────


def verify_export_cmd(args: argparse.Namespace) -> int:
    """Verify integrity of exported chain JSON file (F8).

    Three modes:

      * Operator (HMAC only)::

            bijotel verify-export export.json --secret-hex <hex>

      * Operator + asymmetric attestation::

            bijotel verify-export export.json --secret-hex <hex>
                                              --public-key keys/public.pem

      * Auditor (no secret, public key only) — requires a v2 export::

            bijotel verify-export export.json --public-key keys/public.pem

    The auditor mode proves the operator with the matching private key
    attested to this chain at export time, without ever handing the HMAC
    secret out of band.
    """
    secret = _resolve_secret(args)
    public_key_path = getattr(args, "public_key", None)

    # Credential check BEFORE touching the file — preserves the v1.x
    # exit-code contract: missing credentials returns 2, file errors
    # return 1.
    if secret is None and public_key_path is None:
        print(
            "ERROR: HMAC secret required (--secret-hex or "
            "BIJOTEL_HMAC_SECRET) OR --public-key for auditor mode "
            "against a v2 export — at least one is required.",
            file=sys.stderr,
        )
        return 2

    # Now show what's in the file so the user knows what they're verifying.
    try:
        info = inspect_export(args.path)
    except Exception:  # noqa: BLE001 — defensive; inspect_export rarely raises
        info = {}

    if info.get("error"):
        print(f"ERROR: cannot read export file: {info['error']}", file=sys.stderr)
        return 1
    fmt = info.get("format", "?")
    print(f"File:          {args.path}")
    print(f"  format:        {fmt}")
    print(f"  entries_count: {info.get('entries_count', '?')}")
    print(f"  size:          {info.get('size_bytes', 0):,} bytes")
    if info.get("signed"):
        print(f"  signed by:     {info.get('public_key_fingerprint', '?')} (Ed25519)")
    print()
    if secret is None and public_key_path is not None and fmt != FORMAT_ID_V2_NAME:
        print(
            f"ERROR: auditor mode requires a v2 export; this file is {fmt}. "
            "Either provide --secret-hex or ask the operator to re-export "
            "with --sign-key.",
            file=sys.stderr,
        )
        return 1

    valid, reason = verify_export(
        args.path, secret_key=secret, public_key_path=public_key_path
    )
    if valid:
        if public_key_path and secret is not None:
            print("Export VALID — HMAC chain + Ed25519 signature both verified.")
        elif public_key_path:
            print("Export VALID — Ed25519 signature verified (auditor mode, HMAC not re-checked).")
        else:
            print("Export VALID — HMAC chain verified.")
        return 0

    print(f"Export INVALID: {reason}", file=sys.stderr)
    return 1


# Imported here to avoid a top-of-file circular if processors changes.
from bijotel.processors.export import FORMAT_ID_V2 as FORMAT_ID_V2_NAME  # noqa: E402

# ─────────────────── bijotel regression ────────────────────


def _print_anomalies(dimension: str, anomalies: list[Anomaly]) -> None:
    """Print anomalies tabular for one dimension."""
    if not anomalies:
        print(f"[{dimension}] no anomalies detected")
        return

    print(f"\n[{dimension}] {len(anomalies)} anomalies:")
    print(
        f"  {'seq':<6} {'timestamp':<22} {'value':<14} {'baseline':<14} "
        f"{'z-score':<10} {'iqr-dist':<10} {'severity':<10}"
    )
    print("  " + "-" * 90)
    for a in anomalies:
        z_str = f"{a.z_score:+.2f}" if a.z_score is not None else "N/A"
        iqr_str = f"{a.iqr_distance:+.2f}" if a.iqr_distance is not None else "N/A"
        print(
            f"  {a.seq:<6} {a.timestamp:<22} {a.value:<14.4f} "
            f"{a.baseline_mean:<14.4f} {z_str:<10} {iqr_str:<10} {a.severity:<10}"
        )


def regression_cmd(args: argparse.Namespace) -> int:
    """Run regression detection on chain.db.

    Returns:
        0 if no anomalies detected
        1 if any anomalies detected
        2 if invalid arguments / chain.db unreadable
    """
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: chain DB not found: {db_path}", file=sys.stderr)
        return 2

    detector = RegressionDetector(
        db_path=db_path,
        baseline_window=args.window,
        z_threshold=args.z_threshold,
    )

    total_anomalies = 0
    if args.dimension:
        # Single dimension
        try:
            anomalies = detector.detect(
                dimension=args.dimension,
                filter_model=args.model,
            )
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        _print_anomalies(args.dimension, anomalies)
        total_anomalies = len(anomalies)
    else:
        # All dimensions
        results = detector.detect_all_dimensions(filter_model=args.model)
        for dim, anomalies in results.items():
            _print_anomalies(dim, anomalies)
            total_anomalies += len(anomalies)

    print(f"\nTotal anomalies: {total_anomalies}")
    return 0 if total_anomalies == 0 else 1


# ───────────────────────── serve ─────────────────────────


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
    print(f"ERROR: unknown energy subcommand {sub!r}", file=sys.stderr)
    return 2


def _energy_backfill_cmd(args: argparse.Namespace) -> int:
    import json
    import sqlite3

    from bijotel.layers.energy import CarbonCalculator, EnergyTracker

    db_path = args.db
    if not Path(db_path).is_file():
        print(f"ERROR: chain DB not found at {db_path}", file=sys.stderr)
        return 1

    tracker = EnergyTracker(
        db_path,
        calculator=CarbonCalculator(args.region),
    )

    inserted = 0
    skipped = 0
    failed = 0
    with sqlite3.connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]
        print(f"Backfilling energy from {total} chain entries...")
        for row in conn.execute(
            "SELECT seq, timestamp_ns, canonical_body FROM chain ORDER BY seq"
        ):
            seq, ts_ns, body = row
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
        f"Done. inserted={inserted}, skipped={skipped}, failed={failed}, total={total}"
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


def serve_cmd(args: argparse.Namespace) -> int:
    """Start the BIJOTEL HTTP API server.

    v1.0.0 ships a minimal FastAPI surface (``/health``, ``/version`` plus
    501-placeholder routes for chain / policy / regression). Full endpoints
    arrive in v1.1.0.

    Requires the ``[api]`` extra::

        pip install bijotel[api]

    Exit codes:
        0  — server shut down cleanly (SIGINT / SIGTERM).
        2  — missing fastapi / uvicorn (install bijotel[api]).
        3  — uvicorn raised at startup (port busy, bad host, etc.).
    """
    try:
        from bijotel.api.app import create_app
    except ImportError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        print("\nInstall the API extra:", file=sys.stderr)
        print("    pip install bijotel[api]", file=sys.stderr)
        return 2

    try:
        import uvicorn
    except ImportError:
        print(
            "ERROR: uvicorn not installed. Install with: pip install bijotel[api]",
            file=sys.stderr,
        )
        return 2

    db_path = args.db or os.environ.get("BIJOTEL_DB_PATH", "chain.db")
    serve_dashboard = getattr(args, "dashboard", False)
    app = create_app(db_path=db_path, serve_dashboard=serve_dashboard)

    api_base = "/api" if serve_dashboard else ""
    print(f"BIJOTEL serve: http://{args.host}:{args.port}  (db={db_path})")
    if serve_dashboard:
        print(f"  Dashboard: http://{args.host}:{args.port}/")
    print(f"  Docs:    http://{args.host}:{args.port}{api_base}/docs")
    print(f"  Health:  http://{args.host}:{args.port}{api_base}/health")

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    except Exception as e:  # pragma: no cover - hard to simulate without real port
        print(f"ERROR: uvicorn startup failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    return 0
