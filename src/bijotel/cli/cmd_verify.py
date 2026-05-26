"""``bijotel verify`` / ``verify-export`` / ``verify-continuity`` handlers.

Three commands extracted as a unit because they share the same surface
(verify something on disk, return 0/non-zero). The Ed25519 surface used
by ``verify-export`` was added in v2.1.0; the multi-segment continuity
check arrived in v2.2.0.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from bijotel.cli._helpers import _parse_range_args, _resolve_secret
from bijotel.processors import (
    chain_range_summary,
    inspect_export,
    verify_chain,
    verify_continuity,
    verify_export,
)
from bijotel.processors.export import FORMAT_ID_V2 as FORMAT_ID_V2_NAME


def verify_cmd(args: argparse.Namespace) -> int:
    """Verify chain integrity (full chain or a range)."""
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

    range_kwargs = _parse_range_args(args)
    summary = chain_range_summary(db_path, **range_kwargs)
    valid, seq, reason = verify_chain(db_path, secret, **range_kwargs)
    with sqlite3.connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM chain").fetchone()[0]

    if valid:
        if range_kwargs:
            count = summary["count"]
            first_seq = summary["first_seq"]
            last_seq = summary["last_seq"]
            print(
                f"Chain segment VALID: seq {first_seq}-{last_seq} "
                f"({count} entries of {total} total)."
            )
            if summary["first_prev_hash"] and first_seq and first_seq > 1:
                anchor = (
                    "matches predecessor in same DB"
                    if summary["boundary_predecessor_in_db"]
                    else "boundary — predecessor not in this DB "
                    "(use verify-continuity to confirm cross-segment)"
                )
                print(
                    f"Boundary: prev_hash at seq={first_seq} = "
                    f"{summary['first_prev_hash'][:16]}... ({anchor})"
                )
        else:
            print(f"Chain VALID ({total} entries).")
        return 0
    print(f"Chain BROKEN at seq={seq}: {reason}", file=sys.stderr)
    return 3


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


def verify_continuity_cmd(args: argparse.Namespace) -> int:
    """Verify chain continuity across an ordered list of segment DBs.

    For each adjacent pair ``(A, B)``::

        A.last_hmac_hash == B's first row.prev_hash

    The check is order-sensitive: pass DBs in chronological order
    (oldest archive first, live chain.db last).
    """
    result = verify_continuity(args.dbs)
    print("Segments:")
    for s in result["segments"]:
        ok = "VALID" if s["valid"] else f"INVALID ({s.get('reason', '?')})"
        first = s.get("first_seq")
        last = s.get("last_seq")
        count = s.get("count")
        print(
            f"  {s['db_path']} — {count} entries, seq {first}-{last} — {ok}"
        )
    print()
    print("Boundaries:")
    for b in result["boundaries"]:
        marker = "OK" if b["matches"] else "BREAK"
        print(f"  {b['from_db']} → {b['to_db']}: {marker}")
        if not b["matches"]:
            print(f"    expected (prev segment's last hmac): {b['expected']}")
            print(f"    actual   (next segment's first prev):  {b['actual']}")
            if b.get("reason"):
                print(f"    reason: {b['reason']}")
    print()
    if result["valid"]:
        n_segs = len(result["segments"])
        total = sum(s.get("count") or 0 for s in result["segments"])
        print(f"Full chain: {total} entries across {n_segs} segments, CONTINUOUS.")
        return 0
    print(
        "Continuity verification FAILED — at least one segment or boundary is invalid.",
        file=sys.stderr,
    )
    return 1
