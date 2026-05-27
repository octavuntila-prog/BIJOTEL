"""``bijotel cross-view`` — unified view across multiple BIJOTEL chains.

Example::

    bijotel cross-view \\
        --chain "GENA=/data/bijotel_chain.db" \\
        --chain "ARA=/app/data/bijotel_chain.db" \\
        --json

Pass each chain as ``name=path``. The path can be a chain.db (SQLite)
or a pre-exported JSON file; the loader picks based on suffix
(``.db``, ``.sqlite`` -> DB; everything else -> export JSON).
"""

from __future__ import annotations

import argparse
import json
import sys

from bijotel.cross_view import CrossEcosystemView


def _looks_like_db(path: str) -> bool:
    return path.endswith((".db", ".sqlite", ".sqlite3"))


def add_cross_view_subparser(subparsers) -> None:
    """Register ``bijotel cross-view`` subcommand on the parent parser."""
    p = subparsers.add_parser(
        "cross-view",
        help="Unified view across multiple BIJOTEL chains.",
        description=(
            "Aggregate stats and integrity from N chains. Each chain stays "
            "sovereign — no merging, no rewriting. Read-only."
        ),
    )
    p.add_argument(
        "--chain",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help=(
            "Chain to include. Repeatable. PATH ending in .db/.sqlite "
            "is read as SQLite; otherwise as exported JSON."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the full summary dict as JSON (default: human table).",
    )
    p.add_argument(
        "--integrity",
        action="store_true",
        help=(
            "Run integrity report. Without --hmac-secret per chain, "
            "only a structural check is reported."
        ),
    )


def cross_view_cmd(args: argparse.Namespace) -> int:
    """Handler for ``bijotel cross-view``."""
    view = CrossEcosystemView()

    for spec in args.chain:
        if "=" not in spec:
            print(
                f"ERROR: --chain expects NAME=PATH, got: {spec!r}",
                file=sys.stderr,
            )
            return 2
        name, _, path = spec.partition("=")
        if not name or not path:
            print(
                f"ERROR: invalid --chain spec (empty name or path): {spec!r}",
                file=sys.stderr,
            )
            return 2

        try:
            if _looks_like_db(path):
                view.add_chain(name, db_path=path)
            else:
                view.add_chain(name, export_path=path)
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2

    summary = view.summary()

    if args.integrity:
        summary["integrity_report"] = view.integrity_report()

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=False))
        return 0

    # Human-readable table
    print(f"Ecosystems:    {summary['ecosystems']}")
    print(f"Total entries: {summary['total_entries']:,}")
    print(f"Providers:     {', '.join(summary['total_providers']) or '(none)'}")
    print()
    print(f"{'Name':<12} {'Entries':>10} {'Providers':<24} {'Models':<6}")
    print("-" * 60)
    for name, stats in summary["per_ecosystem"].items():
        provs = ",".join(stats["providers"])[:23]
        nmodels = len(stats["models"])
        print(f"{name:<12} {stats['entries']:>10,} {provs:<24} {nmodels:<6}")

    if args.integrity:
        print()
        print("Integrity:")
        for name, r in summary["integrity_report"]["per_chain"].items():
            marker = "OK" if r["valid"] else "FAIL"
            print(f"  {name}: {marker} (method={r['method']})")
            if "error" in r:
                print(f"    error: {r['error']}")
        cc = summary["integrity_report"]["cross_chain"]
        print(
            f"  timeline_overlap: {cc['timeline_overlap']}, "
            f"shared_providers: {cc['shared_providers'] or '(none)'}"
        )

    return 0
