"""Portable signed JSON export of HMAC chain.

Use case: send the audit trail to an external auditor. They verify integrity
with just the shared HMAC secret + the exported JSON file — no SQLite
database access required.

Schema variants
---------------

``bijotel-chain-v1`` — HMAC-only (shipped v1.1+, default through v2.0.x)::

    {
        "format": "bijotel-chain-v1",
        "chain_signature": "HMAC(secret, 'chain:<head_hash>:<entries_count>')",
        "entries": [...]
    }

``bijotel-chain-v2`` — adds Ed25519 attestation (v2.1.0+). Produced when
``export_chain`` is called with ``sign_key_path``. Backward-compatible:
``verify_export`` accepts both formats. The v2 format ADDS an
``ed25519_signature`` block over the existing v1 contents — nothing is
removed::

    {
        "format": "bijotel-chain-v2",
        "chain_signature": "...",                       (same as v1)
        "ed25519_signature": {
            "public_key": "<base64 raw 32-byte pubkey>",
            "signature":  "<base64 raw 64-byte signature>",
            "signed_over": "chain_signature",
            "algorithm":   "Ed25519"
        },
        "entries": [...]
    }

The Ed25519 signature is computed over the UTF-8 bytes of the
``chain_signature`` hex string. That binds the asymmetric attestation to
the same content the HMAC chain_signature already binds — so a v2 export
gives the auditor two independent integrity proofs (one per-entry HMAC
they can verify if they hold the secret, one outer Ed25519 they can
verify with the public key alone).

Why Ed25519 was added
---------------------

Pre-v2.1.0 the auditor needed the HMAC secret to verify an export. That
created a "shared secret with auditor" trust problem: anyone who can
verify can also forge. v2.1.0 keeps the HMAC chain intact for in-house
verification and adds Ed25519 as the outer attestation layer:

- **Operator** holds the HMAC secret and the Ed25519 *private* key.
- **Auditor** receives the export JSON and the Ed25519 *public* key
  (out of band).
- Auditor can verify "the operator with private key X published this
  chain" without ever holding the seal-time HMAC secret. They cannot
  forge entries.

Verification path
-----------------

For a v1 export with HMAC secret::

    1. Parse JSON; check ``format`` is one of v1/v2.
    2. Recompute ``chain_signature`` from head_hash + entries_count;
       reject mismatch (file-level tamper detection).
    3. For each entry in order: recompute ``hmac_hash`` from
       ``HMAC(prev_hash || canonical_hash, secret)``; reject mismatch.
    4. Verify ``prev_hash`` chain links.
    5. Verify ``canonical_body`` bytes hash to ``canonical_hash``
       (v2.0.3+ — catches body tamper that left HMAC links intact).

For a v2 export with public key (auditor mode, no secret)::

    1. Parse JSON; check ``format == "bijotel-chain-v2"``.
    2. Verify Ed25519 signature over ``chain_signature`` bytes using
       provided public key.
    3. Verify the public key embedded in the file matches the one the
       auditor was given.
    4. Verify each entry's body hash matches its ``canonical_hash``
       (structural check that doesn't need the HMAC secret).
    5. Verify ``prev_hash`` chain links.
    Steps 3–5 + signature give the auditor a complete tamper-evidence
    proof under the operator's public key, without ever holding the
    HMAC secret.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT_ID_V1 = "bijotel-chain-v1"
FORMAT_ID_V2 = "bijotel-chain-v2"
FORMAT_VERSION = "1.0"
GENESIS_HASH = "0" * 64

# ``FORMAT_ID`` retained as backward-compatible alias — some callers
# (and tests) imported it before v2.1.0. New code should prefer the
# explicit V1/V2 constants.
FORMAT_ID = FORMAT_ID_V1


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
    sign_key_path: str | Path | None = None,
    *,
    seq_start: int | None = None,
    seq_end: int | None = None,
    since_ns: int | None = None,
    until_ns: int | None = None,
    last_n: int | None = None,
) -> Path:
    """Export the HMAC chain from SQLite to a portable signed JSON file.

    Args:
        db_path: Path to chain.db (BIJOTEL HmacChainSpanProcessor output).
        output_path: Path where signed JSON will be written.
        secret_key: HMAC secret used to seal entries. The produced
            ``chain_signature`` is computed under this secret and must
            match the secret used at seal time — otherwise the export
            won't verify under ``verify_export``.
        sign_key_path: Optional path to an Ed25519 PEM private key. When
            present, the output uses ``bijotel-chain-v2`` schema and
            includes an asymmetric signature an auditor can verify
            without the HMAC secret. When absent (default), the output
            uses ``bijotel-chain-v1`` (backward-compatible).
        seq_start / seq_end / since_ns / until_ns / last_n: Optional
            range filters (v2.2.0+). When any filter is applied the
            exported file gains a ``segment`` block describing the
            slice (first_seq, last_seq, total_in_full_chain,
            boundary_prev_hash, is_complete_chain=false). Verifiers
            check the slice internally and report the boundary so the
            slice can be cross-checked against an archive.

    Returns:
        ``Path`` to the written file.

    Raises:
        FileNotFoundError: ``db_path`` does not exist, or
            ``sign_key_path`` was provided but doesn't exist.
        sqlite3.OperationalError: db missing chain table.
        ValueError: ``secret_key`` shorter than 16 bytes, or signing
            key is not a valid Ed25519 PEM.
    """
    if len(secret_key) < 16:
        raise ValueError("secret_key must be at least 16 bytes")

    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"chain.db not found: {db_path}")

    output_path = Path(output_path)

    # Detect whether we're exporting a range (segment) or the whole chain.
    range_active = any(
        v is not None for v in (seq_start, seq_end, since_ns, until_ns, last_n)
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Resolve the row window.
        if last_n is not None:
            row = conn.execute(
                "SELECT seq FROM chain ORDER BY seq DESC LIMIT 1 OFFSET ?",
                (max(0, last_n - 1),),
            ).fetchone()
            if row is None:
                row = conn.execute("SELECT MIN(seq) FROM chain").fetchone()
                resolved_start = row[0] if row and row[0] is not None else 1
            else:
                resolved_start = row[0]
            resolved_end: int | None = None
        else:
            resolved_start = seq_start if seq_start is not None else 1
            resolved_end = seq_end

        clauses = ["seq >= ?"]
        params: list[int] = [resolved_start]
        if resolved_end is not None:
            clauses.append("seq <= ?")
            params.append(resolved_end)
        if since_ns is not None:
            clauses.append("timestamp_ns >= ?")
            params.append(since_ns)
        if until_ns is not None:
            clauses.append("timestamp_ns <= ?")
            params.append(until_ns)
        where_sql = " AND ".join(clauses)

        rows = conn.execute(
            f"""SELECT seq, timestamp_ns, trace_id, span_id, span_name, span_kind,
                      canonical_body, canonical_hash, prev_hash, hmac_hash,
                      semantic_body_hash
               FROM chain
               WHERE {where_sql}
               ORDER BY seq""",
            params,
        ).fetchall()

        # Total in full chain — for segment metadata.
        total_in_full = conn.execute(
            "SELECT COUNT(*) FROM chain"
        ).fetchone()[0]

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
    chain_signature = _chain_signature(head_hash, entries_count, secret_key)

    # Build the v1 payload first — v2 just adds a signature block on top.
    payload: dict[str, Any] = {
        "version": FORMAT_VERSION,
        "format": FORMAT_ID_V1,
        "created_at": datetime.now(UTC).isoformat(),
        "entries_count": entries_count,
        "head_hash": head_hash,
        "chain_signature": chain_signature,
        "entries": entries,
    }

    # v2.2.0: segment metadata when a range filter was applied.
    if range_active and entries:
        payload["segment"] = {
            "first_seq": entries[0]["seq"],
            "last_seq": entries[-1]["seq"],
            "total_in_segment": entries_count,
            "total_in_full_chain": total_in_full,
            "boundary_prev_hash": entries[0]["prev_hash"],
            "is_complete_chain": (
                entries[0]["seq"] == 1 and entries[-1]["seq"] == total_in_full
            ),
        }

    if sign_key_path is not None:
        # Lazy-import the crypto module so users without `cryptography`
        # installed can still produce v1 exports. Realistically
        # `cryptography` is already in the dep closure of most BIJOTEL
        # installs (anthropic, opentelemetry, etc.), but the import
        # placement keeps the option explicit.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        from bijotel.crypto.ed25519 import (
            load_private_pem,
            public_key_raw_b64,
            sign,
        )

        private_pem = load_private_pem(sign_key_path)
        try:
            priv = serialization.load_pem_private_key(private_pem, password=None)
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid signing key PEM: {e}") from e
        if not isinstance(priv, Ed25519PrivateKey):
            raise ValueError(
                f"signing key is not Ed25519 (got {type(priv).__name__})"
            )
        public_pem = priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        signature_bytes = sign(chain_signature.encode("ascii"), private_pem)
        payload["format"] = FORMAT_ID_V2
        payload["ed25519_signature"] = {
            "public_key": public_key_raw_b64(public_pem),
            "signature": base64.b64encode(signature_bytes).decode("ascii"),
            "signed_over": "chain_signature",
            "algorithm": "Ed25519",
        }

    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path


def _verify_entries_structure(
    entries: list[dict[str, Any]],
    secret_key: bytes | None,
    *,
    expected_first_prev: str | None = None,
) -> tuple[bool, str | None]:
    """Walk entries, verifying body-hash, chain links, and (if secret
    given) per-entry HMAC. Returns ``(True, None)`` if all entries pass.

    When ``secret_key is None`` (auditor mode), only body-hash and
    chain-link checks run — the auditor cannot recompute HMACs without
    the secret. The Ed25519 signature over chain_signature still binds
    the rest of the file to the operator's attestation.

    ``expected_first_prev`` (v2.2.0+): for segment exports the first
    entry's ``prev_hash`` is the predecessor's hmac_hash, not genesis.
    Callers pass the segment's ``boundary_prev_hash`` here; pass
    ``None`` for full-chain exports and the genesis anchor is used.
    """
    prev_hash = expected_first_prev if expected_first_prev is not None else GENESIS_HASH
    for i, entry in enumerate(entries):
        body_b64 = entry.get("canonical_body_b64")
        if body_b64 is None:
            return False, f"missing canonical_body_b64 at seq={entry.get('seq', i)}"
        try:
            body_bytes = base64.b64decode(body_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            return False, (
                f"canonical_body_b64 not decodable at seq={entry.get('seq', i)}: {e}"
            )
        recomputed_canonical = hashlib.sha256(body_bytes).hexdigest()
        if recomputed_canonical != entry["canonical_hash"]:
            return False, (
                f"canonical_body tampered at seq={entry.get('seq', i)}: "
                f"body hashes to {recomputed_canonical[:16]}... but "
                f"canonical_hash claims {entry['canonical_hash'][:16]}..."
            )

        # Per-entry HMAC verify — only when secret is provided.
        if secret_key is not None:
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


def _verify_ed25519_block(
    data: dict[str, Any],
    public_key_path: str | Path,
) -> tuple[bool, str | None]:
    """Verify the ed25519_signature block against the operator's pubkey.

    Returns ``(True, None)`` on success; ``(False, reason)`` on failure.
    The auditor's own public-key copy MUST match the one embedded in
    the file — otherwise an attacker who can swap both the signature
    and the embedded public key could mint a "valid" file under a key
    the auditor never saw.
    """
    from bijotel.crypto.ed25519 import (
        load_public_pem,
        public_key_raw_b64,
        verify,
    )

    sig_block = data.get("ed25519_signature")
    if not sig_block:
        return False, "ed25519_signature block missing"
    embedded_pub_b64 = sig_block.get("public_key")
    sig_b64 = sig_block.get("signature")
    if not embedded_pub_b64 or not sig_b64:
        return False, "ed25519_signature is missing public_key or signature"
    if sig_block.get("algorithm") != "Ed25519":
        return False, f"unsupported signature algorithm: {sig_block.get('algorithm')!r}"

    try:
        public_pem = load_public_pem(public_key_path)
    except FileNotFoundError as e:
        return False, str(e)

    try:
        auditor_pub_b64 = public_key_raw_b64(public_pem)
    except ValueError as e:
        return False, f"auditor public key invalid: {e}"

    if auditor_pub_b64 != embedded_pub_b64:
        return False, (
            "public key in export does not match auditor's copy "
            "(embedded != provided — possible key-swap attack)"
        )

    try:
        signature_bytes = base64.b64decode(sig_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        return False, f"ed25519 signature not base64-decodable: {e}"

    chain_sig_str = data.get("chain_signature")
    if not isinstance(chain_sig_str, str):
        return False, "chain_signature missing or not a string"

    ok = verify(chain_sig_str.encode("ascii"), signature_bytes, public_pem)
    if not ok:
        return False, "ed25519 signature does NOT match chain_signature"
    return True, None


def verify_export(
    path: str | Path,
    secret_key: bytes | None = None,
    public_key_path: str | Path | None = None,
) -> tuple[bool, str | None]:
    """Verify integrity of an exported chain JSON file.

    Args:
        path: Path to JSON file produced by ``export_chain``.
        secret_key: HMAC secret used to seal the chain. Required for
            full per-entry HMAC verification. When omitted (auditor
            mode), per-entry HMACs are not re-checked — the Ed25519
            signature still binds the chain_signature and body hashes
            still bind the entries. Auditor mode requires
            ``public_key_path`` AND a v2 export.
        public_key_path: Optional path to an Ed25519 PEM public key.
            When provided, the export's ed25519_signature block is
            verified against this key. Required when the file is
            v2 format AND the caller wants asymmetric attestation
            checked (it's the entire point of v2).

    Returns:
        ``(True, None)`` if every requested check passes.
        ``(False, reason)`` on the first failure encountered.

    Backward compatibility:
        Calling ``verify_export(path, secret)`` with the 2-arg form
        still works exactly as in v1.x — full HMAC verify against a
        v1 export. v2 exports also verify under the same call (the
        signature block is ignored if no public key is provided, with
        a structural skip-note in ``reason`` only on failure).
    """
    # secret-length check happens BEFORE file load so the
    # "at least 16 bytes" diagnostic fires even when the path is bogus
    # (preserves v1.x contract — see test_verify_export_secret_key_min_length).
    if secret_key is not None and len(secret_key) < 16:
        return False, "secret_key must be at least 16 bytes"

    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"File not found: {path}"
    except json.JSONDecodeError as e:
        return False, f"JSON parse error: {e}"

    fmt = data.get("format")
    if fmt not in (FORMAT_ID_V1, FORMAT_ID_V2):
        # Phrasing keeps backward compat with v1.x callers that grep
        # for "Not a bijotel-chain-v1 file" — but now mentions v2 too.
        return False, (
            f"Not a bijotel-chain-v1 file (and not v2 either); "
            f"got format={fmt!r}"
        )

    entries = data.get("entries", [])
    entries_count = data.get("entries_count", -1)
    head_hash = data.get("head_hash", "")

    if entries_count != len(entries):
        return False, f"entries_count mismatch ({entries_count} vs len(entries)={len(entries)})"

    # A verify with NEITHER the HMAC secret NOR an Ed25519 public key checks
    # only self-referential structure (body hashes + chain links) — which a
    # forger can satisfy, so it would hand back a false VALID. Refuse, mirroring
    # the CLI (cmd_verify), so the library contract is honest. (audit ISSUE-8)
    if secret_key is None and public_key_path is None:
        return False, (
            "verify requires a trust anchor: pass secret_key (HMAC) or "
            "public_key_path (Ed25519, v2 export). With neither, only "
            "self-referential structure is checked, which a forged export "
            "can satisfy."
        )

    # Reject pure-auditor calls against a v1 export — there's no
    # signature to verify, so the auditor would be relying on nothing.
    if secret_key is None and public_key_path is not None and fmt == FORMAT_ID_V1:
        return False, (
            "auditor-mode verify requires a v2 (signed) export; this file "
            f"is {FORMAT_ID_V1}. Ask the operator to re-export with --sign-key."
        )

    # Step 1: HMAC chain_signature check (operator side only).
    if secret_key is not None:
        expected_sig = _chain_signature(head_hash, entries_count, secret_key)
        if data.get("chain_signature") != expected_sig:
            return False, "chain_signature mismatch — file may be tampered"

    if entries and entries[-1].get("hmac_hash") != head_hash:
        return False, "head_hash does not match last entry hmac_hash"

    # v2.2.0: segment exports anchor the first entry's prev_hash to
    # the boundary recorded at export time, not to GENESIS.
    segment_block = data.get("segment")
    expected_first_prev: str | None = None
    if segment_block:
        expected_first_prev = segment_block.get("boundary_prev_hash")

    # Step 2: Ed25519 signature check (auditor / operator-with-key).
    if public_key_path is not None:
        if fmt != FORMAT_ID_V2:
            return False, (
                f"--public-key provided but file is {fmt} "
                f"(only {FORMAT_ID_V2} carries an Ed25519 signature)"
            )
        ok, reason = _verify_ed25519_block(data, public_key_path)
        if not ok:
            return False, reason

    # Step 3: per-entry walk (body-hash + HMAC if secret + chain links).
    return _verify_entries_structure(
        entries, secret_key, expected_first_prev=expected_first_prev
    )


def inspect_export(path: str | Path) -> dict[str, Any]:
    """Read-only structural inspection of an export file.

    Returns a metadata dict the CLI uses to print what's in the file
    BEFORE attempting verification:

        {
            "format": "bijotel-chain-v2",
            "version": "1.0",
            "entries_count": 6332,
            "head_hash": "abc123...",
            "created_at": "2026-05-26T...",
            "size_bytes": 82_345_678,
            "signed": True,                  # v2 only
            "public_key_b64": "...",         # v2 only
            "public_key_fingerprint": "...", # v2 only — short SHA-256 of pubkey
        }

    Never raises on a malformed file — returns whatever it could parse
    plus ``{"error": "..."}`` if the file is unreadable / not JSON.
    """
    path = Path(path)
    info: dict[str, Any] = {"path": str(path)}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {**info, "error": f"file not found: {path}"}
    except OSError as e:
        return {**info, "error": f"OS error reading file: {e}"}

    info["size_bytes"] = path.stat().st_size
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return {**info, "error": f"JSON parse error: {e}"}

    for key in ("format", "version", "entries_count", "head_hash", "created_at"):
        if key in data:
            info[key] = data[key]

    sig_block = data.get("ed25519_signature")
    if sig_block:
        info["signed"] = True
        info["public_key_b64"] = sig_block.get("public_key")
        # Lightweight fingerprint: SHA-256 of the raw 32-byte pubkey,
        # first 16 hex chars. Used for "signed by 8f0a9c4b..." display.
        try:
            raw_pub = base64.b64decode(sig_block.get("public_key", ""), validate=True)
            info["public_key_fingerprint"] = hashlib.sha256(raw_pub).hexdigest()[:16]
        except (binascii.Error, ValueError):
            info["public_key_fingerprint"] = None
    else:
        info["signed"] = False

    # v2.2.0: segment metadata for range exports.
    segment_block = data.get("segment")
    if segment_block:
        info["segment"] = {
            "first_seq": segment_block.get("first_seq"),
            "last_seq": segment_block.get("last_seq"),
            "total_in_segment": segment_block.get("total_in_segment"),
            "total_in_full_chain": segment_block.get("total_in_full_chain"),
            "boundary_prev_hash": segment_block.get("boundary_prev_hash"),
            "is_complete_chain": segment_block.get("is_complete_chain"),
        }
    return info
