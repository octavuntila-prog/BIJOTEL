"""``bijotel anchor`` — publish + verify Rekor transparency-log anchors.

Two subcommands:

  * ``bijotel anchor publish --db PATH --sign-key PATH [--output PATH]``
        signs the chain head with the operator's Ed25519 key, uploads
        the (hash, signature, public-key) triple to Rekor, prints the
        resulting log_index + UUID, and optionally writes the full
        ``RekorAnchor`` JSON sidecar so an auditor can verify later.

  * ``bijotel anchor verify --anchor-file PATH [--public-key PATH]``
        re-fetches the entry from Rekor by log_index and checks
        (hash, public key, signature) all line up. Exit 0 on match,
        3 on mismatch, 1 on operational error, 2 on bad args.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from bijotel.anchoring import (
    REKOR_PUBLIC_URL,
    RekorAnchor,
    anchor_chain_head,
    verify_rekor_anchor,
)


def anchor_cmd(args: argparse.Namespace) -> int:
    """Dispatch to publish / verify subcommands.

    Exit codes:
        0  — success.
        1  — operational error (chain missing, network, signing).
        2  — argument error.
        3  — verify mismatch (anchor verify only).
    """
    sub = getattr(args, "anchor_command", None)
    if sub == "publish":
        return _anchor_publish_cmd(args)
    if sub == "verify":
        return _anchor_verify_cmd(args)
    print(f"ERROR: unknown anchor subcommand {sub!r}", file=sys.stderr)
    return 2


def _anchor_publish_cmd(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: chain DB not found: {db_path}", file=sys.stderr)
        return 1
    sign_key = Path(args.sign_key)
    if not sign_key.exists():
        print(f"ERROR: signing key not found: {sign_key}", file=sys.stderr)
        return 1

    try:
        anchor = anchor_chain_head(
            db_path,
            sign_key_pem=sign_key,
            rekor_url=args.rekor_url,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"ERROR: anchor publish failed: {e}", file=sys.stderr)
        return 1

    # Human summary on stdout. Auditors save the JSON sidecar.
    print("=== Rekor anchor PUBLISHED ===")
    print(f"  rekor:           {anchor.rekor_url}")
    print(f"  log_index:       {anchor.log_index}")
    print(f"  log_uuid:        {anchor.log_uuid}")
    print(f"  integrated_time: {anchor.integrated_time}")
    print(f"  head_seq:        {anchor.head_seq}")
    print(f"  head_hash:       {anchor.head_hash[:32]}...")
    public_url = (
        f"{anchor.rekor_url.rstrip('/')}/api/v1/log/entries?logIndex="
        f"{anchor.log_index}"
    )
    print(f"  public url:      {public_url}")

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(json.dumps(anchor.to_dict(), indent=2), encoding="utf-8")
        print(f"  sidecar:         {out_path}")
    return 0


def _anchor_verify_cmd(args: argparse.Namespace) -> int:
    anchor_path = Path(args.anchor_file)
    if not anchor_path.exists():
        print(f"ERROR: anchor file not found: {anchor_path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(anchor_path.read_text(encoding="utf-8"))
        # RekorAnchor is a frozen dataclass; we recreate it from JSON.
        anchor = RekorAnchor(
            rekor_url=data["rekor_url"],
            log_index=int(data["log_index"]),
            log_uuid=data["log_uuid"],
            integrated_time=int(data["integrated_time"]),
            head_hash=data["head_hash"],
            head_seq=int(data["head_seq"]),
            signature_b64=data["signature_b64"],
            public_key_pem=data["public_key_pem"],
        )
    except (KeyError, ValueError) as e:
        print(f"ERROR: anchor file malformed: {e}", file=sys.stderr)
        return 1

    expected_pubkey: Path | None = None
    if args.public_key:
        expected_pubkey = Path(args.public_key)
        if not expected_pubkey.exists():
            print(f"ERROR: public key not found: {expected_pubkey}", file=sys.stderr)
            return 1

    result = verify_rekor_anchor(anchor, expected_public_key_pem=expected_pubkey)

    verdict = "MATCH" if result.match else "MISMATCH"
    print(f"=== Rekor anchor {verdict} (logIndex={result.log_index}) ===")
    print(f"  integrated_time:  {result.integrated_time}")
    print(f"  hash_matches:     {result.hash_matches}")
    print(f"  pubkey_matches:   {result.pubkey_matches}")
    print(f"  signature_valid:  {result.signature_valid}")
    if result.reason:
        print(f"  reason:           {result.reason}")

    # Print full result as JSON if --json passed.
    if args.json:
        print()
        print(json.dumps(asdict(result), indent=2))

    return 0 if result.match else 3


def add_anchor_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Wire ``bijotel anchor`` and its publish/verify subparsers.

    Called from ``cli/main.py`` so the dispatcher stays in one place.
    """
    p_anchor = subparsers.add_parser(
        "anchor",
        help=(
            "Publish or verify a Rekor (Sigstore) transparency-log anchor "
            "for the chain head."
        ),
    )
    anchor_sub = p_anchor.add_subparsers(dest="anchor_command", required=True)

    p_pub = anchor_sub.add_parser(
        "publish",
        help=(
            "Sign the chain head with --sign-key and upload to Rekor. "
            "Prints the log_index + UUID; optionally writes a sidecar JSON."
        ),
    )
    p_pub.add_argument("--db", required=True, help="SQLite chain DB path.")
    p_pub.add_argument(
        "--sign-key",
        required=True,
        help="Path to operator's Ed25519 private-key PEM (from `bijotel keygen`).",
    )
    p_pub.add_argument(
        "--output",
        help="Where to write the RekorAnchor JSON sidecar (recommended).",
    )
    p_pub.add_argument(
        "--rekor-url",
        default=REKOR_PUBLIC_URL,
        help=f"Rekor instance URL (default: {REKOR_PUBLIC_URL}).",
    )

    p_ver = anchor_sub.add_parser(
        "verify",
        help=(
            "Fetch the Rekor entry by log_index and confirm "
            "(hash, public key, signature) all match."
        ),
    )
    p_ver.add_argument(
        "anchor_file",
        help="Path to a RekorAnchor JSON sidecar (from `anchor publish`).",
    )
    p_ver.add_argument(
        "--public-key",
        help=(
            "Path to the operator's Ed25519 public-key PEM as an external "
            "trust anchor. Strongly recommended: without this the verifier "
            "trusts the public key stored *inside* the anchor file."
        ),
    )
    p_ver.add_argument(
        "--json",
        action="store_true",
        help="Also print the full AnchorVerifyResult as JSON.",
    )
