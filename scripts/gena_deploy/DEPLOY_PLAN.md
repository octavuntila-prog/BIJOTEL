# GENA Deploy Plan — BIJOTEL one-container-at-a-time

**Target:** Deploy BIJOTEL `HmacChainSpanProcessor` + `CasSpanProcessor` to GENA's
4 LLM-active ecosystems (v3-atelier, v4-piata, v9-oracle, v8-ambasador), in
parallel with existing `substrate_v2_trace.py` monkey-patch (dual-observer).

**Mechanism:** `opentelemetry-instrumentation-anthropic` 0.60.0 wraps Anthropic
SDK class methods; `substrate_v2_trace.py` wraps instance methods via
`__init__` patch. Both observers receive every call independently — verified
empirically in Sub-task 0 (2026-05-10), both FORWARD and REVERSE orders
confirmed working.

## Pre-flight checklist

- [x] Sub-task 0 dual-patch coexistence GO (forward + reverse both pass)
- [x] anthropic 0.40.0 + instrumentation-anthropic 0.60.0 compat confirmed
- [x] Backups created on GENA:
  - `docker-compose.yml.pre-bijotel.<TS>`
  - `requirements.txt.pre-bijotel.<TS>`
  - `ecosystems/v{3,4,9,8}_runner.py.pre-bijotel.<TS>`
- [x] Image tags pinned: `gena-{v3-atelier,v4-piata,v9-oracle,v8-ambasador}:pre-bijotel`
- [x] `BIJOTEL_HMAC_SECRET` (32-byte hex) in `/opt/substrate-v2/.env` (perms 600)
- [x] Local backup of secret: `BIJOTEL/.env-gena` (chmod 600)
- [x] `.env` confirmed in `.gitignore`
- [x] BIJOTEL wheel built (`bijotel-0.0.1-py3-none-any.whl`, 28510 B, sha256 `614612b2...`) and placed at `/opt/substrate-v2/bijotel-0.0.1-py3-none-any.whl`
- [x] Lab dry-run: `patch_runner.py` and `patch_compose.py` both PASS + idempotent (verified on copies, 2026-05-10)

## Apply order (most active first)

1. **v3-atelier** (122 calls / 24h, $1.41 cost/day) — highest LLM volume
2. **v4-piata** (122 calls / 24h, $0.69) — quality eval
3. **v9-oracle** (45 calls / 24h, $0.34) — directives
4. **v8-ambasador** (low volume, broken endpoint) — last; lowest signal/risk

## Per-ecosystem deploy steps

### Step 1: Apply patches (idempotent, on GENA host)

```bash
cd /opt/substrate-v2

# Append upstream deps to requirements.txt (apply once, before any rebuild)
# Idempotent check: grep first
if ! grep -q "opentelemetry-instrumentation-anthropic" requirements.txt; then
    cat /tmp/requirements_addition.txt >> requirements.txt
fi

# Patch runner (idempotent — exits if marker present)
python3 /tmp/patch_runner.py ecosystems/v{N}_runner.py v{N}

# Patch compose (idempotent — skips already-patched services)
python3 /tmp/patch_compose.py docker-compose.yml
```

### Step 2: Place BIJOTEL wheel

```bash
# Track B publishes wheel locally; scp to GENA build context
# (Wheel auto-installed via existing `COPY *.whl /tmp/wheels/` Dockerfile pattern)
scp BIJOTEL/dist/bijotel-<VER>-py3-none-any.whl \
    root@gena:/opt/substrate-v2/
```

### Step 3: Build + recreate single ecosystem

```bash
ssh root@gena "cd /opt/substrate-v2 && \
    docker compose build v{N}-{name} && \
    docker compose up -d v{N}-{name}"
```

### Step 4: Verify (within 60s)

```bash
ssh root@gena "bash -s" << 'EOF'
sleep 30  # ecosystem boot + first signal cycle

# Check 1: container healthy
docker ps --filter name=gena-v{N}-{name} --format '{{.Status}}'
# Expected: "Up X seconds (healthy)"

# Check 2: BIJOTEL init log line present
docker logs gena-v{N}-{name}-1 --since 60s 2>&1 | grep "bijotel:"
# Expected: "[v{N}] bijotel: chain=/data/bijotel_chain.db, instrumentation active"

# Check 3: substrate_v2_trace still works (no regression)
docker logs gena-v{N}-{name}-1 --since 60s 2>&1 | grep "SessionTrace V2:"
# Expected: "[v{N}] SessionTrace V2: AsyncAnthropic patched -> /data/traces.db"

# Check 4: no exceptions in startup
docker logs gena-v{N}-{name}-1 --since 60s 2>&1 | grep -iE "error|exception|traceback" | head -5
# Expected: empty (or only pre-existing benign errors)
EOF
```

### Step 5: Verify spans flow (after first natural LLM call, ~5-10 min)

```bash
ssh root@gena "ls -la /var/lib/docker/volumes/gena_shared-data/_data/bijotel_chain.db"
# Expected: file exists, size > 0

# Inspect first chain entry
ssh root@gena "docker exec gena-v{N}-{name}-1 python3 -c \"
import sqlite3
conn = sqlite3.connect('/data/bijotel_chain.db')
print('chain rows:', conn.execute('SELECT COUNT(*) FROM chain').fetchone()[0])
print('cas rows:', conn.execute('SELECT COUNT(*) FROM cas_blobs').fetchone()[0])
\""

# Verify chain integrity (BIJOTEL CLI, runs from any container with bijotel installed)
ssh root@gena "docker exec gena-v{N}-{name}-1 \
    bijotel verify --db /data/bijotel_chain.db"
# Expected: "Chain VALID. seq=N"
```

## Rollback (per ecosystem, < 2 min)

```bash
ssh root@gena << 'EOF'
cd /opt/substrate-v2
# Revert image
docker compose down v{N}-{name}
docker tag gena-v{N}-{name}:pre-bijotel gena-v{N}-{name}:latest
docker compose up -d v{N}-{name}

# (Optional) revert source files if blocking subsequent rebuilds
# Restore from .pre-bijotel.<TS> backups
EOF
```

## Global rollback (all ecosystems)

```bash
ssh root@gena << 'EOF'
cd /opt/substrate-v2
TS=<latest-backup-timestamp>

# Restore source files
cp docker-compose.yml.pre-bijotel.${TS} docker-compose.yml
cp requirements.txt.pre-bijotel.${TS} requirements.txt
for eco in v3 v4 v9 v8; do
    cp ecosystems/${eco}_runner.py.pre-bijotel.${TS} ecosystems/${eco}_runner.py
done

# Re-tag images back to latest
for eco in v3-atelier v4-piata v9-oracle v8-ambasador; do
    docker tag gena-${eco}:pre-bijotel gena-${eco}:latest
done

# Recreate containers
docker compose up -d v3-atelier v4-piata v9-oracle v8-ambasador
EOF
```

## Stress test (after all 4 deployed)

Goal: verify BIJOTEL doesn't lose spans under load, chain remains valid,
no memory leak after 1000 calls.

Method: from laptop, fire 1000 Haiku calls in parallel batches (cheap ~$0.03).
Verify chain.db has 1000 new rows and `bijotel verify` returns OK.

Acceptance: 1000/1000 spans in chain, verify OK, container memory delta <+20% vs pre-deploy.

## Coexistence test (after all 4 deployed)

Goal: confirm dual observers (substrate_v2_trace + BIJOTEL) BOTH capture every
call in production-like conditions (organic GENA traffic, not synthetic).

Method: snapshot `traces.db` row count (T0), wait 2h, snapshot again (T1).
Same for `bijotel_chain.db`. Diff should be equal: each LLM call written to
both DBs.

Acceptance: `(traces.db rows[T1] - traces.db rows[T0]) == (chain rows[T1] - chain rows[T0])`,
±1 to allow for edge cases at sample boundaries.

## "Stable empirical" criteria for BIJOTEL 0.1.0 release

After 24h of GENA running with BIJOTEL attached:
- 1000+ spans in `bijotel_chain.db`
- `bijotel verify --db chain.db` returns OK across full chain
- CAS dedup factor > 1.5x
- Memory delta < +20% vs pre-BIJOTEL baseline
- Zero unplanned container restarts

If all ✓ → 0.1.0 ship. Otherwise debug.

## Post-deploy state (2026-05-10 09:34 UTC)

**ALL 4 ECOSYSTEMS DEPLOYED** in 45 min cumulative wall-clock.

| Ecosystem | Deploy completion | Synthetic verify | Chain entry seq |
|---|---|---|---|
| v3-atelier | 2026-05-10T09:14:54Z | OK ('ok' response) | seq=1 |
| v4-piata | 2026-05-10T09:19:52Z | OK ('# V4 Test OK ✓') | seq=2 |
| v9-oracle | 2026-05-10T09:20:25Z | OK ('# V9 Test Confirmed ✓') | seq=3 |
| v8-ambasador | 2026-05-10T09:21:01Z | OK ('# V8 Test OK ✓') | seq=4 |

**Memory baseline (4 BIJOTEL containers):**
- v3-atelier: 68.0 MB
- v4-piata: 65.6 MB
- v9-oracle: 67.1 MB
- v8-ambasador: 59.9 MB
- **Estimated overhead vs control group (V1/V2/V5/V6/V7 ~57-58 MB): ~3-10 MB per container**

**Memory baseline (5 control non-LLM ecosystems, no BIJOTEL):**
- v1-digestor: 58.1 MB
- v2-patterns: 57.2 MB
- v5-oglinda: 56.9 MB
- v6-sentinel: 82.8 MB (signal-heavy, separate from BIJOTEL)
- v7-memoria: 57.3 MB

**Pre-deploy 24h cost burn rate** (substrate_v2_trace data):
- v3: 143 calls, $1.66 / 24h ($0.0116/call avg)
- v4: 143 calls, $0.80 / 24h ($0.0056/call avg)
- v9: 52 calls, $0.40 / 24h ($0.0077/call avg)
- **Total: ~$2.86 / 24h** = $0.12/hour run cost

**Chain post-deploy:**
- 7 entries (4 synthetic + 3 organic emerged in 13 min) → confirms organic flow active
- bytes_per_span_avg: 9947 (~10KB per span — full canonical body + HMAC)
- Verify: VALID

**Files:**
- `/data/bijotel_chain.db`: 68 KB
- `/data/traces.db`: 44 KB (continues growing, substrate_v2_trace works in parallel)
- `/data/` total: 130.6 KB

## Daily checkpoint (T+24h, T+72h, etc.)

**Workflow** (~30s):
```bash
cd "BIJOTEL/scripts/gena_deploy"
python compare_baseline.py "<path-to-baseline.json>"
```

**Exit codes:**
- `0` ALL HEALTHY — proceed
- `1` WARNINGS — review output
- `2` CRITICAL — chain broken / memory exploded / fundamental issue

**Thresholds:**
- Memory growth >+30% per BIJOTEL container = WARN
- Tick rate drop >-20% per ecosystem with traffic = WARN (note: noisy at low absolute counts)
- Chain integrity not VALID = CRITICAL
- Chain rows decreased = CRITICAL
- traces.db rows decreased = CRITICAL

**Files:**
- Baseline JSON: `ARTEFACT GENA/GENA 05-10-2026/baseline_post_deploy_2026-05-10.json`
- capture_baseline.py: lives at `/tmp/capture_baseline.py` on GENA (re-deploy if absent)
- compare_baseline.py: local, in `BIJOTEL/scripts/gena_deploy/`

---

## Cost field semantics (BIJOTEL chain vs traces.db)

**Observed during Hour 2.0-3.0 deploy verify (2026-05-10):** `bijotel list` may show `$0.0000` for some spans even when tokens are non-zero (e.g. seq=1 had 14+4 tokens but cost $0.0000, while seq=3 with 13+10 tokens showed $0.0001).

**Why:**
- BIJOTEL chain stores **raw `gen_ai.*` attrs** (model, tokens_in, tokens_out, request/response). It is the canonical observability layer.
- Cost calculation is **on-demand**, computed by `bijotel inspect/list` via internal price table (see `bijotel/cli/commands.py::_calc_cost`).
- substrate_v2_trace.py writes `cost_usd` **pre-calculated** into traces.db at span emit time.
- Inconsistency in `bijotel list` cost ($0.0000 vs $0.0001 for same model) likely a price-lookup edge case (string match for model name) — minor cosmetic issue, not data integrity.

**Implication:**
- For accurate cost reporting → traces.db is authoritative for now (substrate_v2_trace pre-computes).
- For tamper-evident audit trail with full payloads → chain.db is authoritative.
- Future BIJOTEL F8+: improve `_calc_cost` consistency, or move cost calc to processor (write at emit time, like traces.db).

**Capture for next-session reader (1 week from now):** "Why is cost $0 in chain but $0.0001 in traces.db?" — see this section.

---

## BIJOTEL update path (rebuild required)

**Trade-off accepted:** BIJOTEL is installed into the Docker image at build time
via the existing `*.whl drop-in` pattern (`COPY *.whl /tmp/wheels/` +
`pip install /tmp/wheels/*.whl`). Same mechanism as the 5 existing wheels on
GENA (codeslp, edgecompile, hypy, modelfit, sessiontrace).

**Update flow** (any time BIJOTEL code changes):

1. **Build new wheel locally:**
   ```bash
   cd BIJOTEL
   .venv/Scripts/python -m build --wheel
   # Output: dist/bijotel-X.Y.Z-py3-none-any.whl
   ```

2. **SCP to GENA build context:**
   ```bash
   scp dist/bijotel-X.Y.Z-py3-none-any.whl root@gena:/opt/substrate-v2/
   ```

3. **(Optional) remove old wheel** to avoid pip ambiguity if version changed:
   ```bash
   ssh root@gena "rm /opt/substrate-v2/bijotel-OLD.whl"
   ```

4. **Rebuild + recreate target containers:**
   ```bash
   ssh root@gena "cd /opt/substrate-v2 && \
       docker compose build v3-atelier v4-piata v9-oracle v8-ambasador && \
       docker compose up -d --force-recreate v3-atelier v4-piata v9-oracle v8-ambasador"
   ```

**Cycle cost per update:** ~2 min build + ~30s scp + ~30s × 4 rebuilds + ~30s × 4 recreate = ~5-7 min total.

**Why not editable install (`pip install -e /path/to/source`)?**
- Reproducible builds: wheel = artifact with stable SHA256
- Same pattern as 5 existing GENA wheels (consistency)
- No phantom-code risk from editable mode
- Wheel content visible via `unzip -l bijotel-*.whl`

**Why not skip versioning (single canonical wheel)?**
- Each version-bumped wheel keeps build context clean (old wheel can be removed)
- Pip resolves to latest version when multiple are present, but explicit removal of old prevents confusion
- Forward path to PyPI publish if ever needed (no rename surgery)

**Expected ship cadence:**
- 0.0.1 (current, 2026-05-10) — initial deploy
- 0.1.0 (target Friday 2026-05-17) — F7 included, "stable empirical" criteria met

---

## Files in this directory

| File | Purpose |
|---|---|
| `requirements_addition.txt` | Upstream deps to append to `/opt/substrate-v2/requirements.txt` |
| `runner_hook.py.snippet` | Reference snippet (the patch logic, for review) |
| `patch_runner.py` | Idempotent patcher for `v{N}_runner.py` files |
| `compose_patch.yaml` | Reference YAML for compose env addition |
| `patch_compose.py` | Idempotent line-based patcher for `docker-compose.yml` |
| `DEPLOY_PLAN.md` | This file |
