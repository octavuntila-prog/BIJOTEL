"""Tests for portable signed JSON export of HMAC chain (F8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.processors import (
    HmacChainSpanProcessor,
    export_chain,
    verify_export,
)

SECRET = b"x" * 32


@pytest.fixture
def chain_db(tmp_path: Path) -> Path:
    """Build a chain.db with 3 spans by emitting via real TracerProvider."""
    db = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(HmacChainSpanProcessor(db_path=db, secret_key=SECRET))
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test")
    for i in range(3):
        with tracer.start_as_current_span(f"test-span-{i}") as span:
            span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            span.set_attribute("gen_ai.usage.input_tokens", 10 + i)
            span.set_attribute("gen_ai.usage.output_tokens", 5)

    provider.shutdown()
    return db


def test_export_chain_creates_valid_json(chain_db: Path, tmp_path: Path) -> None:
    """export_chain produces a valid JSON file with expected schema."""
    out = tmp_path / "audit.json"
    result = export_chain(chain_db, out, SECRET)

    assert result == out
    assert out.exists()

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["format"] == "bijotel-chain-v1"
    assert data["version"] == "1.0"
    assert data["entries_count"] == 3
    assert len(data["entries"]) == 3
    assert "head_hash" in data
    assert "chain_signature" in data
    assert "created_at" in data


def test_export_then_verify_roundtrip(chain_db: Path, tmp_path: Path) -> None:
    """Roundtrip: export then verify_export — ALL valid."""
    out = tmp_path / "audit.json"
    export_chain(chain_db, out, SECRET)

    valid, reason = verify_export(out, SECRET)
    assert valid is True, f"Export should verify: {reason}"
    assert reason is None


def test_verify_export_detects_tampered_entry(
    chain_db: Path, tmp_path: Path
) -> None:
    """Modifying any entry's canonical_hash breaks verify."""
    out = tmp_path / "audit.json"
    export_chain(chain_db, out, SECRET)

    data = json.loads(out.read_text(encoding="utf-8"))
    # Tamper: flip a char in canonical_hash of entry 1
    original = data["entries"][1]["canonical_hash"]
    data["entries"][1]["canonical_hash"] = "0" + original[1:]
    out.write_text(json.dumps(data), encoding="utf-8")

    valid, reason = verify_export(out, SECRET)
    assert valid is False
    assert "hmac_hash mismatch" in (reason or "")


def test_verify_export_detects_tampered_chain_signature(
    chain_db: Path, tmp_path: Path
) -> None:
    """Modifying chain_signature alone (without recomputing) breaks verify."""
    out = tmp_path / "audit.json"
    export_chain(chain_db, out, SECRET)

    data = json.loads(out.read_text(encoding="utf-8"))
    data["chain_signature"] = "0" * 64
    out.write_text(json.dumps(data), encoding="utf-8")

    valid, reason = verify_export(out, SECRET)
    assert valid is False
    assert "chain_signature mismatch" in (reason or "")


def test_verify_export_detects_inserted_entry(
    chain_db: Path, tmp_path: Path
) -> None:
    """Inserting a fake entry (with valid-looking hashes) breaks chain."""
    out = tmp_path / "audit.json"
    export_chain(chain_db, out, SECRET)

    data = json.loads(out.read_text(encoding="utf-8"))
    # Insert a duplicate entry — chain links will break
    fake = dict(data["entries"][0])
    fake["seq"] = 999
    data["entries"].insert(1, fake)
    data["entries_count"] = len(data["entries"])
    out.write_text(json.dumps(data), encoding="utf-8")

    valid, reason = verify_export(out, SECRET)
    assert valid is False
    # Either chain_signature catches it (head_hash now wrong) or per-entry HMAC fails
    assert reason is not None


def test_verify_export_detects_wrong_secret(
    chain_db: Path, tmp_path: Path
) -> None:
    """Verifying with a different secret fails (chain_signature first)."""
    out = tmp_path / "audit.json"
    export_chain(chain_db, out, SECRET)

    wrong_secret = b"y" * 32
    valid, reason = verify_export(out, wrong_secret)
    assert valid is False
    assert "chain_signature mismatch" in (reason or "")


def test_verify_export_rejects_wrong_format(tmp_path: Path) -> None:
    """File without correct format identifier rejected."""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"format": "some-other-format", "entries": []}), encoding="utf-8"
    )

    valid, reason = verify_export(bad, SECRET)
    assert valid is False
    assert "Not a bijotel-chain-v1 file" in (reason or "")


def test_verify_export_rejects_missing_file(tmp_path: Path) -> None:
    """Nonexistent file → clean error, not crash."""
    valid, reason = verify_export(tmp_path / "doesnotexist.json", SECRET)
    assert valid is False
    assert "File not found" in (reason or "")


def test_verify_export_rejects_invalid_json(tmp_path: Path) -> None:
    """Malformed JSON → clean error, not crash."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")

    valid, reason = verify_export(bad, SECRET)
    assert valid is False
    assert "JSON parse error" in (reason or "")


def test_export_chain_secret_key_min_length() -> None:
    """secret_key < 16 bytes raises ValueError."""
    with pytest.raises(ValueError, match="at least 16 bytes"):
        export_chain("nonexistent.db", "out.json", b"short")


def test_verify_export_secret_key_min_length(tmp_path: Path) -> None:
    """verify_export with short secret returns clean error, no crash."""
    valid, reason = verify_export(tmp_path / "any.json", b"short")
    assert valid is False
    assert "at least 16 bytes" in (reason or "")


def test_export_chain_handles_empty_db(tmp_path: Path) -> None:
    """Exporting an empty chain produces valid file with entries_count=0."""
    db = tmp_path / "empty.db"
    # Init DB but don't emit any spans
    HmacChainSpanProcessor(db_path=db, secret_key=SECRET)

    out = tmp_path / "audit.json"
    export_chain(db, out, SECRET)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["entries_count"] == 0
    assert data["entries"] == []
    assert data["head_hash"] == "0" * 64

    valid, reason = verify_export(out, SECRET)
    assert valid is True, f"Empty export should verify: {reason}"
