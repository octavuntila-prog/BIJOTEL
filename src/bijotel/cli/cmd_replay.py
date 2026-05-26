"""``bijotel replay`` — verify a replayed LLM output against the chain.

Reads one chain entry by ``seq``, takes a replayed output string (either
inline via ``--output`` or from ``--output-file``), and compares the
SHA-256 of the replay against the sealed ``bijotel.replay.output_hash``.

This command does NOT call any LLM. The caller is expected to have
already re-executed the original prompt with the same model/seed/
temperature and produced ``replayed_output`` separately. BIJOTEL only
proves whether the hash matches — the rest is the caller's pipeline.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from bijotel.cli._helpers import _parse_canonical_body
from bijotel.replay import verify_replay


def replay_cmd(args: argparse.Namespace) -> int:
    """Compare a sealed output hash against a replayed output.

    Exit codes:
        0  — replay matched the sealed hash.
        1  — chain DB missing, seq not found, or output source unreadable.
        2  — argument errors.
        3  — mismatch (replay differs from sealed).
    """
    if not args.output and not args.output_file:
        print(
            "ERROR: pass exactly one of --output 'text' or --output-file PATH.",
            file=sys.stderr,
        )
        return 2
    if args.output and args.output_file:
        print(
            "ERROR: --output and --output-file are mutually exclusive.",
            file=sys.stderr,
        )
        return 2

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: chain DB not found: {db_path}", file=sys.stderr)
        return 1

    # Resolve the replayed output bytes.
    if args.output_file:
        out_path = Path(args.output_file)
        if not out_path.exists():
            print(f"ERROR: --output-file not found: {out_path}", file=sys.stderr)
            return 1
        try:
            replayed_output = out_path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"ERROR: cannot read --output-file: {e}", file=sys.stderr)
            return 1
    else:
        replayed_output = args.output

    # Load the chain entry.
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT canonical_body FROM chain WHERE seq = ?", (int(args.seq),)
        ).fetchone()
        if not row:
            print(f"ERROR: seq {args.seq} not found in chain", file=sys.stderr)
            return 1

    body_dict = _parse_canonical_body(row[0])
    result = verify_replay(body_dict, replayed_output)

    # Pretty-print the verdict.
    verdict = "MATCH" if result.match else "MISMATCH"
    print(f"=== Replay {verdict} (seq={args.seq}) ===")
    orig_str = result.original_hash or "(none — entry has no replay metadata)"
    print(f"  original_hash:  {orig_str}")
    print(f"  replay_hash:    {result.replay_hash}")
    print(f"  deterministic:  {result.deterministic}")
    print(f"  model_version:  {result.model_version or '(unknown)'}")
    if result.reason:
        print(f"  reason:         {result.reason}")

    return 0 if result.match else 3
