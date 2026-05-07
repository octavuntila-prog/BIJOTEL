"""BIJOTEL CLI entry point."""

from __future__ import annotations

import argparse
import sys

from bijotel.cli.commands import inspect_cmd, list_cmd, stats_cmd, verify_cmd


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bijotel",
        description="BIJOTEL CLI: audit chain verify, inspect, stats, list.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # bijotel verify
    p_verify = subparsers.add_parser(
        "verify", help="Verify chain integrity (HMAC + canonical hashes)."
    )
    p_verify.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_verify.add_argument(
        "--secret-hex",
        help="HMAC secret as hex string. Default: env BIJOTEL_HMAC_SECRET.",
    )

    # bijotel inspect
    p_inspect = subparsers.add_parser(
        "inspect", help="Show full canonical body + CAS info for a span."
    )
    p_inspect.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_inspect.add_argument(
        "id",
        help="Span ID (16-hex) OR chain seq (integer).",
    )

    # bijotel stats
    p_stats = subparsers.add_parser(
        "stats", help="Summary stats: chain, CAS, policy state."
    )
    p_stats.add_argument("--db", required=True, help="SQLite chain DB path.")

    # bijotel list
    p_list = subparsers.add_parser("list", help="List spans with optional filters.")
    p_list.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_list.add_argument(
        "--blocked",
        action="store_true",
        help="Only spans with bijotel.blocked=true.",
    )
    p_list.add_argument(
        "--rule",
        help="Filter by policy rule name (e.g. cost_per_call_max).",
    )
    p_list.add_argument(
        "--model", help="Filter by gen_ai.request.model exact match."
    )
    p_list.add_argument(
        "--since",
        help="Calendar date UTC (YYYY-MM-DD). Lower bound 00:00:00Z.",
    )
    p_list.add_argument(
        "--limit", type=int, default=50, help="Max rows (default 50)."
    )
    p_list.add_argument(
        "--offset", type=int, default=0, help="Skip N rows."
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    handlers = {
        "verify": verify_cmd,
        "inspect": inspect_cmd,
        "stats": stats_cmd,
        "list": list_cmd,
    }
    handler = handlers[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
