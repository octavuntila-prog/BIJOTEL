"""``bijotel federation`` — operator-side CLI for chain federation.

Four subcommands:

  * ``register --service URL --public-key PATH --private-key PATH --org NAME``
        register the operator with a federation. Two-call
        challenge-response: federation issues a nonce, operator signs
        it with the private key. Receipt written to stdout / sidecar.

  * ``submit --service URL --operator-id ID --private-key PATH --export PATH``
        submit a ``bijotel-chain-v2`` signed export. Federation
        verifies the signature, runs continuity check, returns a
        submission receipt.

  * ``verify --receipt PATH [--federation-key PATH]``
        local-only verify of a cross-anchor receipt JSON file. No
        network call. Confirms the federation Ed25519 signature and
        recomputes the cross-anchor hash from participating operators.

  * ``status --service URL``
        unauthenticated health/discovery query.

Useful even before any federation service exists: ``--dry-run`` on
``register`` and ``submit`` produces the payload locally so operators
can mail it to a future service or test their client wiring.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bijotel.federation import (
    CrossAnchorReceipt,
    FederationClient,
    verify_cross_anchor_receipt,
)


def federation_cmd(args: argparse.Namespace) -> int:
    """Dispatch to one of register / submit / verify / status."""
    sub = getattr(args, "federation_command", None)
    if sub == "register":
        return _federation_register_cmd(args)
    if sub == "submit":
        return _federation_submit_cmd(args)
    if sub == "verify":
        return _federation_verify_cmd(args)
    if sub == "status":
        return _federation_status_cmd(args)
    print(f"ERROR: unknown federation subcommand {sub!r}", file=sys.stderr)
    return 2


def _federation_register_cmd(args: argparse.Namespace) -> int:
    pub_path = Path(args.public_key)
    priv_path = Path(args.private_key)
    if not pub_path.exists():
        print(f"ERROR: public key not found: {pub_path}", file=sys.stderr)
        return 1
    if not priv_path.exists():
        print(f"ERROR: private key not found: {priv_path}", file=sys.stderr)
        return 1

    pub_pem = pub_path.read_bytes()
    priv_pem = priv_path.read_bytes()

    if args.dry_run:
        # No network — just emit the payload an operator would send.
        payload = {
            "public_key_pem": pub_pem.decode("ascii"),
            "org_name": args.org,
            "contact_email": args.contact or "",
            "note": "DRY RUN — challenge-response not performed",
        }
        print(json.dumps(payload, indent=2))
        return 0

    client = FederationClient(args.service)
    try:
        receipt = client.register(
            public_key_pem=pub_pem,
            private_key_pem=priv_pem,
            org_name=args.org,
            contact_email=args.contact or "",
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print("=== Federation registration OK ===")
    print(f"  operator_id:       {receipt.get('operator_id')}")
    print(f"  registered_at:     {receipt.get('registered_at')}")
    print(f"  rekor_log_index:   {receipt.get('rekor_log_index')}")
    if args.output:
        Path(args.output).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(f"  receipt sidecar:   {args.output}")
    return 0


def _federation_submit_cmd(args: argparse.Namespace) -> int:
    export_path = Path(args.export)
    priv_path = Path(args.private_key)
    if not export_path.exists():
        print(f"ERROR: export not found: {export_path}", file=sys.stderr)
        return 1
    if not priv_path.exists():
        print(f"ERROR: private key not found: {priv_path}", file=sys.stderr)
        return 1

    export_text = export_path.read_text(encoding="utf-8")
    try:
        export_obj = json.loads(export_text)
    except json.JSONDecodeError as e:
        print(f"ERROR: export is not valid JSON: {e}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps({
            "operator_id": args.operator_id,
            "submission_envelope": export_obj,
            "note": "DRY RUN — would POST to /federation/submit",
        }, indent=2)[:500] + "\n...")
        return 0

    client = FederationClient(args.service)
    try:
        receipt = client.submit(
            operator_id=args.operator_id,
            private_key_pem=priv_path.read_bytes(),
            signed_export_json=export_text,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print("=== Federation submission OK ===")
    print(f"  submission_id:        {receipt.get('submission_id')}")
    print(f"  verified:             {receipt.get('verified')}")
    print(f"  entry_count:          {receipt.get('entry_count')}")
    print(f"  continuity_verified:  {receipt.get('continuity_verified')}")
    print(f"  pending_anchor:       {receipt.get('pending_anchor')}")
    if args.output:
        Path(args.output).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(f"  receipt sidecar:      {args.output}")
    return 0


def _federation_verify_cmd(args: argparse.Namespace) -> int:
    receipt_path = Path(args.receipt)
    if not receipt_path.exists():
        print(f"ERROR: receipt file not found: {receipt_path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(receipt_path.read_text(encoding="utf-8"))
        # Reconstruct dataclass; tolerate missing optional fields.
        receipt = CrossAnchorReceipt(
            anchor_id=data["anchor_id"],
            cross_anchor_hash=data["cross_anchor_hash"],
            participating_operators=data["participating_operators"],
            anchored_at=data["anchored_at"],
            rekor_log_index=data.get("rekor_log_index"),
            rekor_url=data.get("rekor_url"),
            federation_signature=data["federation_signature"],
            federation_public_key_pem=data["federation_public_key_pem"],
        )
    except (KeyError, ValueError) as e:
        print(f"ERROR: receipt malformed: {e}", file=sys.stderr)
        return 1

    expected_pubkey = None
    if args.federation_key:
        expected_pubkey = Path(args.federation_key).read_bytes()

    result = verify_cross_anchor_receipt(receipt, federation_public_key_pem=expected_pubkey)

    verdict = "MATCH" if result["valid"] else "MISMATCH"
    print(f"=== Federation receipt {verdict} (anchor_id={receipt.anchor_id}) ===")
    for k, v in result["checks"].items():
        print(f"  {k}:  {v}")
    print(f"  anchored_at:                      {receipt.anchored_at}")
    print(f"  participating_operators:          {len(receipt.participating_operators)}")
    if receipt.rekor_log_index:
        print(f"  rekor_log_index:                  {receipt.rekor_log_index}")
    if result["reason"]:
        print(f"  reason:                           {result['reason']}")

    if args.json:
        print()
        print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 3


def _federation_status_cmd(args: argparse.Namespace) -> int:
    client = FederationClient(args.service)
    try:
        data = client.status()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2))
    return 0


def add_federation_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Wire ``bijotel federation register/submit/verify/status``.

    Called from ``cli/main.py`` so the dispatch lives in one place.
    """
    p_fed = subparsers.add_parser(
        "federation",
        help=(
            "Client for the BIJOTEL chain-federation service (v2.11.0). "
            "Register, submit signed exports, verify cross-anchor receipts."
        ),
    )
    fed_sub = p_fed.add_subparsers(dest="federation_command", required=True)

    # register
    p_reg = fed_sub.add_parser("register", help="Register with a federation service.")
    p_reg.add_argument("--service", help="Federation base URL.")
    p_reg.add_argument("--public-key", required=True, help="Ed25519 public PEM path.")
    p_reg.add_argument("--private-key", required=True, help="Ed25519 private PEM path.")
    p_reg.add_argument("--org", required=True, help="Organisation name.")
    p_reg.add_argument("--contact", help="Contact email (optional).")
    p_reg.add_argument("--output", help="Write RegistrationReceipt JSON here.")
    p_reg.add_argument(
        "--dry-run",
        action="store_true",
        help="Emit the registration payload locally — no network call.",
    )

    # submit
    p_sub = fed_sub.add_parser("submit", help="Submit a signed export.")
    p_sub.add_argument("--service", help="Federation base URL.")
    p_sub.add_argument("--operator-id", required=True, help="Your operator_id from register.")
    p_sub.add_argument("--private-key", required=True, help="Ed25519 private PEM path.")
    p_sub.add_argument("--export", required=True, help="bijotel-chain-v2 JSON path.")
    p_sub.add_argument("--output", help="Write SubmissionReceipt JSON here.")
    p_sub.add_argument(
        "--dry-run",
        action="store_true",
        help="Emit the submission envelope locally — no network call.",
    )

    # verify (LOCAL ONLY — works without service)
    p_ver = fed_sub.add_parser(
        "verify",
        help=(
            "Locally verify a cross-anchor receipt (no network). "
            "Recomputes the cross-anchor hash + checks the federation "
            "Ed25519 signature."
        ),
    )
    p_ver.add_argument("receipt", help="Path to CrossAnchorReceipt JSON.")
    p_ver.add_argument(
        "--federation-key",
        help=(
            "Path to expected federation public PEM (external trust "
            "anchor). Strongly recommended."
        ),
    )
    p_ver.add_argument(
        "--json",
        action="store_true",
        help="Also print the full verify result as JSON.",
    )

    # status
    p_stat = fed_sub.add_parser("status", help="Federation status (unauthenticated).")
    p_stat.add_argument("--service", required=True, help="Federation base URL.")
