"""
Idempotent patcher for v{N}_runner.py files on GENA.

Applies the bijotel_hook (runner_hook.py.snippet) AFTER the substrate_v2_trace
try/except block, BEFORE the `if __name__ == "__main__":` line.

Idempotency: detects existing `# bijotel_hook_v1` marker, exits clean if present.

Usage:
    python patch_runner.py /opt/substrate-v2/ecosystems/v3_runner.py v3
    python patch_runner.py /opt/substrate-v2/ecosystems/v4_runner.py v4
    python patch_runner.py /opt/substrate-v2/ecosystems/v9_runner.py v9
    python patch_runner.py /opt/substrate-v2/ecosystems/v8_runner.py v8

Per-runner: ECOSYSTEM_NAME placeholder in snippet replaced with arg2.
"""
from pathlib import Path
import sys

if len(sys.argv) != 3:
    print("Usage: python patch_runner.py <runner.py path> <ecosystem_short_name>", file=sys.stderr)
    print("  ecosystem_short_name: v3 | v4 | v9 | v8", file=sys.stderr)
    sys.exit(1)

runner_path = Path(sys.argv[1])
eco_name = sys.argv[2]

if not runner_path.exists():
    print(f"ERROR: runner not found: {runner_path}", file=sys.stderr)
    sys.exit(1)

src = runner_path.read_text(encoding="utf-8")
MARKER = "# bijotel_hook_v1"

if MARKER in src:
    print(f"Already patched (marker found in {runner_path}) -- exit clean")
    sys.exit(0)

# Snippet template (mirrors runner_hook.py.snippet)
snippet = '''
# --- BIJOTEL integration (added 2026-05-10) --- # bijotel_hook_v1
try:
    import os
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
    from bijotel.processors import HmacChainSpanProcessor, CasSpanProcessor

    _bijotel_secret = os.environ.get("BIJOTEL_HMAC_SECRET")
    if not _bijotel_secret:
        raise RuntimeError("BIJOTEL_HMAC_SECRET env var not set")
    _bijotel_secret_bytes = bytes.fromhex(_bijotel_secret)
    _bijotel_db = "/data/bijotel_chain.db"

    _bijotel_provider = TracerProvider()
    _bijotel_provider.add_span_processor(HmacChainSpanProcessor(
        db_path=_bijotel_db,
        secret_key=_bijotel_secret_bytes,
    ))
    _bijotel_provider.add_span_processor(CasSpanProcessor(db_path=_bijotel_db))
    trace.set_tracer_provider(_bijotel_provider)

    AnthropicInstrumentor().instrument()
    print(f"[__ECOSYSTEM_NAME__] bijotel: chain={_bijotel_db}, instrumentation active")
except ImportError as e:
    print(f"[__ECOSYSTEM_NAME__] bijotel: not available ({e})")
except Exception as e:
    print(f"[__ECOSYSTEM_NAME__] bijotel: init error: {e}")

'''.replace("__ECOSYSTEM_NAME__", eco_name)

# Anchor: `if __name__ == "__main__":` line (insertion point — INSERT BEFORE)
anchor = 'if __name__ == "__main__":'
if anchor not in src:
    print(f"ERROR: anchor not found: {anchor!r}", file=sys.stderr)
    sys.exit(1)

new_src = src.replace(anchor, snippet + anchor, 1)

runner_path.write_text(new_src, encoding="utf-8")
print(f"Patched {runner_path}")
print(f"  Old size: {len(src)} bytes")
print(f"  New size: {len(new_src)} bytes")
print(f"  Diff: +{len(new_src) - len(src)} bytes")
