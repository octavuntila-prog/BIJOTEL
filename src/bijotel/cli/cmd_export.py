"""``bijotel export`` — chain.db → portable signed JSON file (F8).

With ``--sign-key PATH``, the export additionally embeds an Ed25519
signature so auditors can verify it without holding the HMAC secret
(v2.1.0+). Without the flag the output is the same v1 format BIJOTEL
has shipped since v1.1. The v2.2.0 range flags (``--since`` /
``--until`` / ``--range`` / ``--last``) produce segment exports with
boundary metadata.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bijotel.cli._helpers import _parse_range_args, _resolve_secret
from bijotel.processors import export_chain, inspect_export


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
        range_kwargs = _parse_range_args(args)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        out = export_chain(
            db_path, args.output, secret,
            sign_key_path=sign_key_path,
            **range_kwargs,
        )
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
