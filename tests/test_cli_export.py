"""CLI-level tests for `bijotel export` and `bijotel verify-export` (F8 + v0.2.1).

Complements tests/test_processors_export.py (which tests module functions).
This file exercises the argparse + command dispatch + stdout/stderr paths.
"""

from __future__ import annotations

import io
import json
import secrets
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.cli.main import main as cli_main
from bijotel.processors import HmacChainSpanProcessor

SECRET_BYTES = secrets.token_bytes(32)
SECRET_HEX = SECRET_BYTES.hex()


@pytest.fixture
def small_chain(tmp_path: Path) -> Path:
    """Build a small chain with 2 spans for CLI export tests."""
    db_path = tmp_path / "chain.db"
    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET_BYTES)
    )
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test")
    for _i in range(2):
        with tracer.start_as_current_span("anthropic.chat") as s:
            s.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
            s.set_attribute("gen_ai.usage.input_tokens", 10)
            s.set_attribute("gen_ai.usage.output_tokens", 5)

    provider.shutdown()
    return db_path


def test_export_cmd_writes_valid_file(
    small_chain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bijotel export --db chain.db -o out.json` writes a valid JSON file."""
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    out_path = tmp_path / "audit.json"

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(
            ["export", "--db", str(small_chain), "--output", str(out_path)]
        )
    assert rc == 0
    assert out_path.exists()
    assert "Exported chain" in out.getvalue()

    # File is parseable + has expected schema
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["format"] == "bijotel-chain-v1"
    assert data["entries_count"] == 2


def test_export_cmd_missing_secret(
    small_chain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No secret in env or flag → exit 2 + error message."""
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)
    out_path = tmp_path / "audit.json"

    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(
            ["export", "--db", str(small_chain), "--output", str(out_path)]
        )
    assert rc == 2
    assert "secret" in err.getvalue().lower()


def test_export_cmd_missing_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nonexistent --db → exit 2 + clear error."""
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)

    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(
            [
                "export",
                "--db",
                str(tmp_path / "nonexistent.db"),
                "--output",
                str(tmp_path / "out.json"),
            ]
        )
    assert rc == 2
    assert "not found" in err.getvalue().lower()


def test_export_cmd_secret_via_flag(
    small_chain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--secret-hex flag works as alternative to env var."""
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)
    out_path = tmp_path / "audit.json"

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(
            [
                "export",
                "--db",
                str(small_chain),
                "--output",
                str(out_path),
                "--secret-hex",
                SECRET_HEX,
            ]
        )
    assert rc == 0
    assert out_path.exists()


def test_verify_export_cmd_valid(
    small_chain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`bijotel verify-export <path>` returns 0 on valid file."""
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    out_path = tmp_path / "audit.json"

    # First export
    cli_main(["export", "--db", str(small_chain), "--output", str(out_path)])

    # Then verify
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(["verify-export", str(out_path)])
    assert rc == 0
    assert "VALID" in out.getvalue()


def test_verify_export_cmd_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tampered file → exit 1 + reason in stderr."""
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(
        json.dumps({"format": "wrong-format", "entries": []}), encoding="utf-8"
    )

    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(["verify-export", str(bad_path)])
    assert rc == 1
    assert "INVALID" in err.getvalue()


def test_verify_export_cmd_missing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No secret → exit 2."""
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)

    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(["verify-export", str(tmp_path / "any.json")])
    assert rc == 2
    assert "secret" in err.getvalue().lower()


def test_export_cmd_invalid_hex_secret(
    small_chain: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--secret-hex with invalid hex string → exit 2 (graceful)."""
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)

    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit) as exc_info:
        cli_main(
            [
                "export",
                "--db",
                str(small_chain),
                "--output",
                str(tmp_path / "out.json"),
                "--secret-hex",
                "not-valid-hex-zzz",
            ]
        )
    # SystemExit raised by _resolve_secret on invalid hex
    assert exc_info.value.code == 2
