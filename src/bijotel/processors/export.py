"""Portable signed JSON export of HMAC chain.

Use case: send the audit trail to an external auditor. They verify integrity
with just the shared HMAC secret + the exported JSON file — no SQLite
database access required.

Schema: ``bijotel-chain-v1``::

    {
        "version": "1.0",
        "format": "bijotel-chain-v1",
        "created_at": "2026-05-10T...",
        "entries_count": 13,
        "head_hash": "<last hmac_hash in chain>",
        "chain_signature": "HMAC(secret, 'chain:<head_hash>:<entries_count>')",
        "entries": [
            {
                "seq": 1,
                "timestamp_ns": 1715...,
                "trace_id": "...",
                "span_id": "...",
                "span_name": "anthropic.chat",
                "span_kind": "CLIENT",
                "canonical_body_b64": "<base64 of bytes>",
                "canonical_hash": "...",
                "semantic_body_hash": "...",
                "prev_hash": "...",
                "hmac_hash": "..."
            },
            ...
        ]
    }

Pattern adapted from substrate-guard's `chain.py::export()` /
`verify_export()` (separate project at `89.167.66.225`, read-only access
2026-05-10). Differences from upstream:
- Schema includes BIJOTEL-specific fields (canonical_body BLOB base64-encoded,
  semantic_body_hash for CAS cross-reference).
- Reads from SQLite source-of-truth, not in-memory list.
- HMAC computation re-uses bijotel.processors.hmac_chain logic for parity.

Verification path:
    1. Parse JSON; check ``format`` field matches ``bijotel-chain-v1``.
    2. Recompute ``chain_signature`` from ``head_hash`` + ``entries_count``;
       reject if mismatch (file-level tamper detection).
    3. For each entry in order: recompute ``hmac_hash`` from
       ``HMAC(prev_hash || canonical_hash, secret)``; reject if mismatch
       (per-entry tamper detection).
    4. Verify ``prev_hash`` chain links (entry[N].prev_hash == entry[N-1].hmac_hash).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT_ID = "bijotel-chain-v1"
FORMAT_VERSION = "1.0"
GENESIS_HASH = "0" * 64


def _chain_signature(head_hash: str, entries_count: int, secret: bytes) -> str:
    """Compute HMAC-SHA256 over ``chain:<head_hash>:<entries_count>``.

    Provides file-level integrity check: any mutation of head_hash or
    entries_count without recomputing the signature is detected.
    """
    payload = f"chain:{head_hash}:{entries_count}".encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _recompute_hmac(prev_hash: str, canonical_hash: str, secret: bytes) -> str:
    """Recompute hmac_hash for an entry — must match bijotel.processors.hmac_chain."""
    payload = f"{prev_hash}{canonical_hash}".encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def export_chain(
    db_path: str | Path,
    output_path: str | Path,
    secret_key: bytes,
) -> Path:
    """Export the HMAC chain from SQLite to a portable signed JSON file.

    Args:
        db_path: Path to chain.db (BIJOTEL HmacChainSpanProcessor output).
        output_path: Path where signed JSON will be written.
        secret_key: HMAC secret used to sign the chain (must match the
            secret originally used to seal entries — otherwise produced
            signature won't verify).

    Returns:
        ``Path`` to the written file.

    Raises:
        FileNotFoundError: db_path does not exist.
        sqlite3.OperationalError: db missing chain table.
        ValueError: secret_key shorter than 16 bytes.
    """
    if len(secret_key) < 16:
        raise ValueError("secret_key must be at least 16 bytes")

    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"chain.db not found: {db_path}")

    output_path = Path(output_path)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT seq, timestamp_ns, trace_id, span_id, span_name, span_kind,
                      canonical_body, canonical_hash, prev_hash, hmac_hash,
                      semantic_body_hash
               FROM chain
               ORDER BY seq"""
        ).fetchall()

    entries: list[dict[str, Any]] = []
    for row in rows:
        canonical_body_bytes = row["canonical_body"]
        if not isinstance(canonical_body_bytes, (bytes, bytearray)):
            # SQLite BLOB sometimes returned as str depending on driver
            canonical_body_bytes = bytes(canonical_body_bytes, "utf-8")  # type: ignore[arg-type]
        entries.append(
            {
                "seq": row["seq"],
                "timestamp_ns": row["timestamp_ns"],
                "trace_id": row["trace_id"],
                "span_id": row["span_id"],
                "span_name": row["span_name"],
                "span_kind": row["span_kind"],
                "canonical_body_b64": base64.b64encode(canonical_body_bytes).decode("ascii"),
                "canonical_hash": row["canonical_hash"],
                "semantic_body_hash": row["semantic_body_hash"],
                "prev_hash": row["prev_hash"],
                "hmac_hash": row["hmac_hash"],
            }
        )

    head_hash = entries[-1]["hmac_hash"] if entries else GENESIS_HASH
    entries_count = len(entries)

    payload = {
        "version": FORMAT_VERSION,
        "format": FORMAT_ID,
        "created_at": datetime.now(UTC).isoformat(),
        "entries_count": entries_count,
        "head_hash": head_hash,
        "chain_signature": _chain_signature(head_hash, entries_count, secret_key),
        "entries": entries,
    }

    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path


def verify_export(
    path: str | Path,
    secret_key: bytes,
) -> tuple[bool, str | None]:
    """Verify integrity of an exported chain JSON file.

    Args:
        path: Path to JSON file produced by ``export_chain``.
        secret_key: HMAC secret. Must match the one used at seal time AND
            export time — otherwise verification fails.

    Returns:
        ``(True, None)`` if valid. ``(False, reason)`` if invalid; reason
        describes the first integrity failure encountered.

    Verification order (fail-fast):
        1. File parseable as JSON.
        2. ``format`` field matches ``bijotel-chain-v1``.
        3. ``chain_signature`` matches recomputed value.
        4. For each entry: ``hmac_hash`` matches recomputed value.
        5. For each entry N>0: ``prev_hash[N]`` == ``hmac_hash[N-1]``.
    """
    if len(secret_key) < 16:
        return False, "secret_key must be at least 16 bytes"

    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"File not found: {path}"
    except json.JSONDecodeError as e:
        return False, f"JSON parse error: {e}"

    if data.get("format") != FORMAT_ID:
        return False, f"Not a {FORMAT_ID} file (got format={data.get('format')!r})"

    entries = data.get("entries", [])
    entries_count = data.get("entries_count", -1)
    head_hash = data.get("head_hash", "")

    if entries_count != len(entries):
        return False, f"entries_count mismatch ({entries_count} vs len(entries)={len(entries)})"

    expected_sig = _chain_signature(head_hash, entries_count, secret_key)
    if data.get("chain_signature") != expected_sig:
        return False, "chain_signature mismatch — file may be tampered"

    if entries and entries[-1].get("hmac_hash") != head_hash:
        return False, "head_hash does not match last entry hmac_hash"

    prev_hash = GENESIS_HASH
    for i, entry in enumerate(entries):
        # Per-entry HMAC verify
        recomputed = _recompute_hmac(
            entry["prev_hash"], entry["canonical_hash"], secret_key
        )
        if entry["hmac_hash"] != recomputed:
            return False, f"hmac_hash mismatch at seq={entry.get('seq', i)}"

        # Chain link verify
        if entry["prev_hash"] != prev_hash:
            return False, f"prev_hash chain break at seq={entry.get('seq', i)}"
        prev_hash = entry["hmac_hash"]

    return True, None
