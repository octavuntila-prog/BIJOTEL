#!/usr/bin/env bash
# launch_smoke.sh — end-to-end smoke test for a fresh BIJOTEL install.
#
# Validates the v1.4.0 launch claim: `pip install "bijotel[api]"` →
# `bijotel serve --dashboard` produces a working API + UI in one shot.
#
# Idempotent: re-running it cleans up its own scratch dir and re-seeds.
# Runs in a fresh venv so the host's site-packages are untouched.
#
# Usage:
#   bash scripts/launch_smoke.sh [PORT]
#   PORT defaults to 9123 (avoids common collisions).

set -euo pipefail

PORT="${1:-9123}"
TMP="$(mktemp -d -t bijotel-smoke-XXXXXX)"
SERVER_PID=""
trap 'echo; echo "---- cleanup ----"; [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null || true; rm -rf "$TMP"' EXIT

# Path translation: on Git Bash / MSYS, /tmp/... is meaningful to bash but
# *not* to the Windows-native python interpreter inside the venv. cygpath
# converts to a native Windows path the venv python can open.
to_native() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s' "$1"
    fi
}

echo "==== launch_smoke.sh ===="
echo "scratch dir: $TMP"
echo "port:        $PORT"
echo

# ── 1. Fresh venv ──────────────────────────────────────────────────────
python -m venv "$TMP/venv"
# Activate path works for both bash-on-linux and Git-Bash-on-Windows.
PY="$TMP/venv/bin/python"
[ -x "$PY" ] || PY="$TMP/venv/Scripts/python.exe"
BIJOTEL="$TMP/venv/bin/bijotel"
[ -x "$BIJOTEL" ] || BIJOTEL="$TMP/venv/Scripts/bijotel.exe"
echo "→ Python:  $($PY --version)"

# ── 2. Install from PyPI ───────────────────────────────────────────────
echo
echo "→ pip install 'bijotel[api]' from PyPI"
"$PY" -m pip install --quiet --no-cache-dir "bijotel[api]"
"$PY" -c "import bijotel; print(f'   installed bijotel {bijotel.__version__}')"

# ── 3. Seed a chain ────────────────────────────────────────────────────
echo
echo "→ Seeding a chain with one synthetic span"
DB_BASH="$TMP/chain.db"           # path the bash side uses
DB_NATIVE="$(to_native "$DB_BASH")"  # path the Windows python interpreter expects
export BIJOTEL_HMAC_SECRET="$(${PY} -c 'import secrets; print(secrets.token_hex(32))')"
export BIJOTEL_DB_PATH="$DB_NATIVE"
"$PY" - <<'PYEOF'
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from bijotel.processors import HmacChainSpanProcessor

db_path = os.environ["BIJOTEL_DB_PATH"]
provider = TracerProvider()
provider.add_span_processor(
    HmacChainSpanProcessor(
        secret_key=bytes.fromhex(os.environ["BIJOTEL_HMAC_SECRET"]),
        db_path=db_path,
    )
)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("smoke")
with tracer.start_as_current_span("anthropic.chat") as span:
    span.set_attribute("gen_ai.request.model", "claude-haiku-4-5-20251001")
    span.set_attribute("gen_ai.usage.input_tokens", 10)
    span.set_attribute("gen_ai.usage.output_tokens", 5)
provider.shutdown()
print(f"   chain.db seeded with 1 span at {db_path}")
PYEOF

# ── 4. CLI verify ──────────────────────────────────────────────────────
echo
echo "→ bijotel verify"
"$BIJOTEL" verify --db "$DB_NATIVE"

# ── 5. Start the server in --dashboard mode ────────────────────────────
echo
echo "→ bijotel serve --dashboard (port $PORT)"
"$BIJOTEL" serve --host 127.0.0.1 --port "$PORT" --db "$DB_NATIVE" --dashboard \
    >"$TMP/serve.log" 2>&1 &
SERVER_PID=$!
# Wait for the port to be reachable
for _ in $(seq 1 30); do
    if curl --silent --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null; then
        break
    fi
    sleep 0.3
done

# ── 6. Probe the surface ───────────────────────────────────────────────
PASS=0
FAIL=0
check() {
    local label="$1"
    local url="$2"
    local expect="${3:-200}"
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" "$url" || echo "000")
    if [ "$code" = "$expect" ]; then
        printf "   ✓ %-32s %s\n" "$label" "$code"
        PASS=$((PASS+1))
    else
        printf "   ✗ %-32s %s (expected %s)\n" "$label" "$code" "$expect"
        FAIL=$((FAIL+1))
    fi
}

echo
echo "→ Probing endpoints"
check "GET /            (dashboard)"  "http://127.0.0.1:$PORT/"             200
check "GET /health      (root)"        "http://127.0.0.1:$PORT/health"       200
check "GET /api/health  (api mirror)"  "http://127.0.0.1:$PORT/api/health"   200
check "GET /api/version"              "http://127.0.0.1:$PORT/api/version"  200
check "GET /api/chain/stats"          "http://127.0.0.1:$PORT/api/chain/stats" 200
check "GET /api/policy/rules"         "http://127.0.0.1:$PORT/api/policy/rules" 200
check "GET /api/layers"               "http://127.0.0.1:$PORT/api/layers"   200
check "GET /docs"                     "http://127.0.0.1:$PORT/docs"         200
check "GET /openapi.json"             "http://127.0.0.1:$PORT/openapi.json" 200
check "GET /system  (SPA fallback)"   "http://127.0.0.1:$PORT/system"       200

echo
echo "==== summary ===="
echo "  passed: $PASS"
echo "  failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then
    echo
    echo "Server log (last 30 lines):"
    tail -n 30 "$TMP/serve.log"
    exit 1
fi
echo "  ALL GREEN"
