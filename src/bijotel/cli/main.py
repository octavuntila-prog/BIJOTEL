"""BIJOTEL CLI entry point."""

from __future__ import annotations

import argparse
import sys

from bijotel.cli.commands import (
    export_cmd,
    inspect_cmd,
    list_cmd,
    regression_cmd,
    serve_cmd,
    stats_cmd,
    verify_cmd,
    verify_export_cmd,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bijotel",
        description=(
            "BIJOTEL CLI: forensic audit chain — verify, inspect, stats, list, "
            "export, regression, serve."
        ),
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

    # bijotel export
    p_export = subparsers.add_parser(
        "export",
        help="Export chain to portable signed JSON file (verifiable by external auditors).",
    )
    p_export.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_export.add_argument(
        "--output", "-o", required=True, help="Output JSON file path."
    )
    p_export.add_argument(
        "--secret-hex",
        help="HMAC secret as hex string. Default: env BIJOTEL_HMAC_SECRET.",
    )

    # bijotel verify-export
    p_vexp = subparsers.add_parser(
        "verify-export",
        help="Verify integrity of an exported chain JSON file.",
    )
    p_vexp.add_argument("path", help="Path to exported JSON file.")
    p_vexp.add_argument(
        "--secret-hex",
        help="HMAC secret as hex string. Default: env BIJOTEL_HMAC_SECRET.",
    )

    # bijotel regression
    p_reg = subparsers.add_parser(
        "regression",
        help="Detect drift in token usage / cost via z-score + IQR (F12).",
    )
    p_reg.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_reg.add_argument(
        "--dimension",
        choices=["input_tokens", "output_tokens", "cost"],
        help="Single dimension. Default: scan all 3.",
    )
    p_reg.add_argument(
        "--model",
        help="Filter by gen_ai.request.model exact match.",
    )
    p_reg.add_argument(
        "--window",
        type=int,
        default=100,
        help="Baseline window size (number of recent spans, default 100).",
    )
    p_reg.add_argument(
        "--z-threshold",
        type=float,
        default=3.0,
        help="z-score absolute threshold for anomaly (default 3.0).",
    )

    # bijotel serve
    p_serve = subparsers.add_parser(
        "serve",
        help="Start the BIJOTEL HTTP API server (FastAPI + uvicorn).",
    )
    p_serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default 127.0.0.1; use 0.0.0.0 for all interfaces).",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Bind port (default 8080).",
    )
    p_serve.add_argument(
        "--db",
        default=None,
        help="Chain DB path (default: $BIJOTEL_DB_PATH or chain.db).",
    )
    p_serve.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level (default info).",
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
        "export": export_cmd,
        "verify-export": verify_export_cmd,
        "regression": regression_cmd,
        "serve": serve_cmd,
    }
    handler = handlers[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
