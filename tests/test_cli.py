"""Tests pentru CLI subcommands.

Strategy: invocăm main(argv) direct (NOT subprocess) pentru speed + coverage.
Subprocess test e doar smoke pentru console_script entry point — folosim full
path la script-ul din venv (sys.executable parent) ca să fim independenți de PATH.
"""

from __future__ import annotations

import contextlib
import io
import os
import secrets
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from bijotel.cli.main import main as cli_main
from bijotel.policy import Decision, PolicyDeniedError, guard
from bijotel.processors import CasSpanProcessor, HmacChainSpanProcessor

SECRET_BYTES = secrets.token_bytes(32)
SECRET_HEX = SECRET_BYTES.hex()


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    """DB cu 2 spans allowed + 1 blocked."""
    db_path = tmp_path / "test_chain.db"

    provider = TracerProvider()
    provider.add_span_processor(
        HmacChainSpanProcessor(db_path=db_path, secret_key=SECRET_BYTES)
    )
    provider.add_span_processor(CasSpanProcessor(db_path=db_path))
    trace.set_tracer_provider(provider)

    tracer = trace.get_tracer("test")

    # 2 allowed spans (anthropic.chat-style)
    for i in range(2):
        with tracer.start_as_current_span("anthropic.chat") as span:
            span.set_attribute("gen_ai.provider.name", "anthropic")
            span.set_attribute(
                "gen_ai.request.model", "claude-haiku-4-5-20251001"
            )
            span.set_attribute("gen_ai.usage.input_tokens", 10)
            span.set_attribute("gen_ai.usage.output_tokens", 5)
            span.set_attribute(
                "gen_ai.input.messages",
                f'[{{"role":"user","parts":[{{"type":"text","content":"call_{i}"}}]}}]',
            )

    # 1 blocked span via guard
    def fake_fn(**_: dict) -> dict:
        return {}

    def deny_rule(_r: dict) -> Decision:
        return Decision.deny(rule="test_rule", reason="test denied")

    guarded = guard(fake_fn, policy=[deny_rule])
    with contextlib.suppress(PolicyDeniedError):
        guarded(
            model="claude-haiku-4-5-20251001",
            messages=[{"role": "user", "content": "denied call"}],
            max_tokens=10,
        )

    provider.shutdown()
    return db_path


# ─── verify tests ───


def test_verify_ok(populated_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIJOTEL_HMAC_SECRET", SECRET_HEX)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(["verify", "--db", str(populated_db)])
    assert rc == 0
    assert "Chain VALID" in out.getvalue()


def test_verify_missing_secret(
    populated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(["verify", "--db", str(populated_db)])
    assert rc == 2
    assert "secret" in err.getvalue().lower()


def test_verify_secret_via_flag(
    populated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BIJOTEL_HMAC_SECRET", raising=False)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(
            ["verify", "--db", str(populated_db), "--secret-hex", SECRET_HEX]
        )
    assert rc == 0


def test_verify_wrong_secret(populated_db: Path) -> None:
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(
            [
                "verify",
                "--db",
                str(populated_db),
                "--secret-hex",
                "00" * 32,
            ]
        )
    assert rc == 3
    assert "BROKEN" in err.getvalue()


def test_verify_db_not_found() -> None:
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(
            [
                "verify",
                "--db",
                "/nonexistent.db",
                "--secret-hex",
                SECRET_HEX,
            ]
        )
    assert rc == 1


# ─── inspect tests ───


def test_inspect_by_seq(populated_db: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(["inspect", "--db", str(populated_db), "1"])
    assert rc == 0
    assert "Span Metadata" in out.getvalue()
    assert "Status:" in out.getvalue()
    assert "Canonical Body" in out.getvalue()


def test_inspect_by_span_id(populated_db: Path) -> None:
    with sqlite3.connect(populated_db) as conn:
        span_id = conn.execute(
            "SELECT span_id FROM chain WHERE seq = 1"
        ).fetchone()[0]

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(["inspect", "--db", str(populated_db), span_id])
    assert rc == 0
    assert "Span Metadata" in out.getvalue()


def test_inspect_not_found(populated_db: Path) -> None:
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(["inspect", "--db", str(populated_db), "9999"])
    assert rc == 1


# ─── stats tests ───


def test_stats_breakdown(populated_db: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(["stats", "--db", str(populated_db)])
    assert rc == 0
    output = out.getvalue()
    assert "Chain" in output
    assert "Total spans:" in output
    assert "ALLOWED:" in output
    assert "BLOCKED:" in output


# ─── list tests ───


def test_list_default(populated_db: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(["list", "--db", str(populated_db)])
    assert rc == 0
    output = out.getvalue()
    assert "seq" in output
    assert "status" in output


def test_list_blocked_filter(populated_db: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(["list", "--db", str(populated_db), "--blocked"])
    assert rc == 0
    output = out.getvalue()
    assert "BLOCKED" in output
    assert "ALLOWED" not in output


def test_list_rule_filter(populated_db: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(
            ["list", "--db", str(populated_db), "--rule", "test_rule"]
        )
    assert rc == 0
    assert "BLOCKED" in out.getvalue()


def test_list_no_match(populated_db: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(
            ["list", "--db", str(populated_db), "--rule", "nonexistent"]
        )
    assert rc == 0
    assert "no spans match" in out.getvalue()


def test_list_invalid_since(populated_db: Path) -> None:
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cli_main(
            ["list", "--db", str(populated_db), "--since", "not-a-date"]
        )
    assert rc == 2


def test_list_limit_offset(populated_db: Path) -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli_main(["list", "--db", str(populated_db), "--limit", "1"])
    assert rc == 0
    output = out.getvalue()
    assert "(1 spans)" in output


# ─── subprocess smoke test (entry point) ───


def _find_bijotel_script() -> Path | None:
    """Find bijotel console_script in same venv ca pytest's interpreter."""
    venv_scripts = Path(sys.executable).parent
    for name in ("bijotel.exe", "bijotel"):
        candidate = venv_scripts / name
        if candidate.exists():
            return candidate
    return None


def test_cli_entry_point_via_subprocess(populated_db: Path) -> None:
    """Verify `bijotel` console_script e instalat și callable."""
    bijotel_path = _find_bijotel_script()
    if bijotel_path is None:
        pytest.skip(
            f"bijotel script not found in {Path(sys.executable).parent}"
        )

    env = os.environ.copy()
    env["BIJOTEL_HMAC_SECRET"] = SECRET_HEX
    result = subprocess.run(
        [str(bijotel_path), "verify", "--db", str(populated_db)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Chain VALID" in result.stdout
