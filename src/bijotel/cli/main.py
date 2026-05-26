"""BIJOTEL CLI entry point."""

from __future__ import annotations

import argparse
import sys

from bijotel.cli.cmd_anchor import add_anchor_subparser
from bijotel.cli.commands import (
    anchor_cmd,
    archive_cmd,
    energy_cmd,
    export_cmd,
    inspect_cmd,
    integrity_cmd,
    keygen_cmd,
    list_cmd,
    regression_cmd,
    replay_cmd,
    serve_cmd,
    stats_cmd,
    verify_cmd,
    verify_continuity_cmd,
    verify_export_cmd,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bijotel",
        description=(
            "BIJOTEL CLI: tamper-evident HMAC audit chain — verify, inspect, "
            "stats, list, export, regression, serve."
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
    p_verify.add_argument(
        "--since",
        help="Verify only entries on or after this date (YYYY-MM-DD UTC).",
    )
    p_verify.add_argument(
        "--until",
        help="Verify only entries on or before this date (YYYY-MM-DD UTC).",
    )
    p_verify.add_argument(
        "--range",
        help=(
            "Verify only seq A:B (inclusive). Pass A: to mean 'A to end' "
            "or :B to mean 'start to B'."
        ),
    )
    p_verify.add_argument(
        "--last",
        type=int,
        help="Verify only the last N entries by seq.",
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
    p_export.add_argument(
        "--sign-key",
        help=(
            "Path to an Ed25519 PEM private key. When supplied, the export "
            "additionally embeds an asymmetric signature so auditors can "
            "verify without holding the HMAC secret (produces v2 format). "
            "Generate a key with `bijotel keygen`."
        ),
    )
    p_export.add_argument(
        "--since",
        help="Export only entries on or after this date (YYYY-MM-DD UTC).",
    )
    p_export.add_argument(
        "--until",
        help="Export only entries on or before this date (YYYY-MM-DD UTC).",
    )
    p_export.add_argument(
        "--range",
        help="Export only seq A:B (inclusive).",
    )
    p_export.add_argument(
        "--last",
        type=int,
        help="Export only the last N entries by seq.",
    )

    # bijotel keygen
    p_keygen = subparsers.add_parser(
        "keygen",
        help="Generate Ed25519 keypair for signing chain exports.",
    )
    p_keygen.add_argument(
        "--output-dir",
        default="./keys",
        help="Directory to write keys into (default ./keys). "
        "Creates bijotel_private.pem (mode 0600) and bijotel_public.pem.",
    )
    p_keygen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing private key file. Don't use this unless "
        "you understand that old signed exports will no longer match.",
    )

    # bijotel archive
    p_arch = subparsers.add_parser(
        "archive",
        help="Peel oldest chain entries off into a separate archive DB.",
    )
    p_arch.add_argument("--db", required=True, help="Source chain DB.")
    p_arch.add_argument(
        "--output", "-o", required=True,
        help="Destination archive SQLite DB path (must not exist).",
    )
    p_arch.add_argument(
        "--secret-hex",
        help="HMAC secret. Default: env BIJOTEL_HMAC_SECRET.",
    )
    p_arch.add_argument(
        "--before",
        help=(
            "Archive entries with timestamp before this UTC date "
            "(YYYY-MM-DD). Exclusive lower bound."
        ),
    )
    p_arch.add_argument(
        "--before-seq",
        help="Archive entries with seq < this integer. Mutually exclusive with --before.",
    )
    p_arch.add_argument(
        "--sign-key",
        help=(
            "Optional Ed25519 PEM private key. When supplied, a signed "
            "JSON sidecar export of the archive slice is written next to "
            "the archive DB (or to the explicit path)."
        ),
    )
    p_arch.add_argument(
        "--attest",
        choices=["software", "tpm2", "nitro", "gcp", "sgx"],
        help=(
            "Produce a TEE attestation quote alongside the archive (v2.10.0). "
            "Writes <output>.attestation.json next to the archive DB. "
            "Only `software` is functional today (Ed25519 + code measurement "
            "+ platform info); tpm2/nitro/gcp/sgx are stubs that raise with "
            "deployment hints. Requires --sign-key."
        ),
    )
    p_arch.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be archived without writing anything.",
    )

    # bijotel verify-continuity
    p_vcont = subparsers.add_parser(
        "verify-continuity",
        help="Verify hmac-hash continuity across a list of chain DBs.",
    )
    p_vcont.add_argument(
        "dbs",
        nargs="+",
        help=(
            "Two or more chain DB paths, in chronological order "
            "(oldest archive first, live chain.db last)."
        ),
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
    p_vexp.add_argument(
        "--public-key",
        help=(
            "Path to an Ed25519 PEM public key for asymmetric attestation "
            "(v2 exports only). With this flag alone (no --secret-hex), "
            "runs auditor mode: verifies the export's Ed25519 signature + "
            "entry structure without ever needing the HMAC seal-time secret."
        ),
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

    # bijotel energy
    p_energy = subparsers.add_parser(
        "energy",
        help="AI energy + carbon accounting (Bijuteria #3). Subcommands: backfill, summary.",
    )
    energy_sub = p_energy.add_subparsers(dest="energy_command", required=True)

    # bijotel energy backfill --db ...
    p_backfill = energy_sub.add_parser(
        "backfill",
        help="Backfill energy_log from chain.db entries (idempotent on chain.seq).",
    )
    p_backfill.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_backfill.add_argument(
        "--region",
        default="us-east",
        help="Grid region for carbon intensity (default us-east).",
    )

    # bijotel energy summary --db ...
    p_esum = energy_sub.add_parser(
        "summary",
        help="Print aggregate energy + CO2 stats from energy_log.",
    )
    p_esum.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_esum.add_argument(
        "--since",
        help="ISO-8601 lower bound (e.g. 2026-05-24T00:00:00Z).",
    )
    p_esum.add_argument(
        "--until",
        help="ISO-8601 upper bound.",
    )
    p_esum.add_argument(
        "--agent-id",
        help="Filter to one agent.",
    )

    # bijotel anchor publish/verify (v2.9.0)
    add_anchor_subparser(subparsers)

    # bijotel integrity (v2.8.0)
    p_integrity = subparsers.add_parser(
        "integrity",
        help=(
            "Run chain-integrity anomaly detection: sequence gaps, "
            "timestamp anomalies, hash duplicates, provider drift, "
            "rate changes. Exits 1 when any anomaly is detected."
        ),
    )
    p_integrity.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_integrity.add_argument(
        "--window",
        type=int,
        default=100,
        help="How many recent entries to analyze (default 100).",
    )
    p_integrity.add_argument(
        "--json",
        action="store_true",
        help="Emit one compact JSON line (cron-friendly) instead of human output.",
    )

    # bijotel replay (v2.7.0)
    p_replay = subparsers.add_parser(
        "replay",
        help=(
            "Compare a replayed LLM output against the sealed output_hash "
            "of a chain entry. Does NOT call any LLM — caller provides "
            "the replayed text via --output or --output-file."
        ),
    )
    p_replay.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_replay.add_argument(
        "--seq",
        required=True,
        type=int,
        help="Chain seq of the entry to verify against.",
    )
    p_replay.add_argument(
        "--output",
        help="The replayed output text, inline.",
    )
    p_replay.add_argument(
        "--output-file",
        help="Path to a file containing the replayed output (UTF-8).",
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
    p_serve.add_argument(
        "--dashboard",
        action="store_true",
        help=(
            "Mount the prebuilt React dashboard at /. API routes shift to "
            "/api/* in this mode. Requires the dashboard bundle to be "
            "present at src/bijotel/dashboard_dist/ — included in the "
            "official PyPI wheel."
        ),
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
        "keygen": keygen_cmd,
        "archive": archive_cmd,
        "verify-continuity": verify_continuity_cmd,
        "verify-export": verify_export_cmd,
        "regression": regression_cmd,
        "integrity": integrity_cmd,
        "anchor": anchor_cmd,
        "replay": replay_cmd,
        "energy": energy_cmd,
        "serve": serve_cmd,
    }
    handler = handlers[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
