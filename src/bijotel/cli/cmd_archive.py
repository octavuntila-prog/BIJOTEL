"""``bijotel archive`` — chain segmentation + archival (v2.2.0)."""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from bijotel.cli._helpers import _resolve_secret
from bijotel.processors import archive_chain


def archive_cmd(args: argparse.Namespace) -> int:
    """Peel oldest entries off chain.db into a separate archive DB.

    Builds the archive in a fresh SQLite file, verifies the slice
    cleanly, then deletes the archived rows from the source DB in a
    single transaction and VACUUMs to reclaim space.

    Safety:
      * --dry-run reports what would be archived without writing the
        archive DB or deleting from source.
      * The archive is built and verified BEFORE any DELETE runs. If
        verify fails, the source DB is untouched.

    Exit codes:
        0  — archive (or dry-run) completed successfully.
        1  — file errors (source missing, archive path exists, etc).
        2  — argument errors.
        3  — verify failed (source DB left untouched).
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
        return 1

    before_seq: int | None = None
    before_ns: int | None = None
    if args.before_seq:
        try:
            before_seq = int(args.before_seq)
        except ValueError:
            print(f"ERROR: --before-seq must be an integer, got {args.before_seq!r}",
                  file=sys.stderr)
            return 2
    if args.before:
        try:
            dt = datetime.datetime.strptime(args.before, "%Y-%m-%d").replace(
                tzinfo=datetime.UTC
            )
        except ValueError:
            print(f"ERROR: --before must be YYYY-MM-DD, got {args.before!r}",
                  file=sys.stderr)
            return 2
        before_ns = int(dt.timestamp() * 1e9)
    if (before_seq is None) == (before_ns is None):
        print(
            "ERROR: pass exactly one of --before <YYYY-MM-DD> or "
            "--before-seq <int>.",
            file=sys.stderr,
        )
        return 2

    sign_key = getattr(args, "sign_key", None)
    if sign_key and not Path(sign_key).exists():
        print(f"ERROR: --sign-key path not found: {sign_key}", file=sys.stderr)
        return 2

    archive_path = Path(args.output)
    try:
        report = archive_chain(
            db_path,
            archive_path,
            secret,
            before_ns=before_ns,
            before_seq=before_seq,
            sign_key_path=sign_key,
            dry_run=bool(args.dry_run),
        )
    except FileExistsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"ERROR: verify failed during archive: {e}", file=sys.stderr)
        return 3

    if report["dry_run"]:
        print("DRY RUN — no changes made.")
    else:
        print(f"Archived {report['archived_count']} entries to {report['archive_path']}")
    print(f"  Window:        seq {report['first_seq']} → {report['last_seq']}")
    print(f"  first prev:    {report['first_prev_hash'][:16]}...")
    print(f"  last hmac:     {report['last_hmac_hash'][:16]}...")
    if report["boundary_next_prev_hash"]:
        print(
            f"  Boundary OK:   next row's prev = "
            f"{report['boundary_next_prev_hash'][:16]}... (matches last hmac of archive)"
        )
    print(f"  Source DB remaining: {report['main_remaining_count']} entries")
    if report.get("segment_json_path"):
        print(f"  Signed JSON sidecar: {report['segment_json_path']}")
    return 0
