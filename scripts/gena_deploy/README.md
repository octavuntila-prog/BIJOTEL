# scripts/gena_deploy/ — historical artifacts

These files document the first BIJOTEL deploy onto the GENA agent
ecosystem on **2026-05-10**. They are **historical / forensic** — not
maintained as ongoing tooling.

## Contents

| File | Purpose |
|------|---------|
| `DEPLOY_PLAN.md` | Step-by-step plan written before the Day 1 deploy. |
| `capture_baseline.py` | One-shot baseline metrics snapshot (pre-deploy). |
| `compare_baseline.py` | One-shot diff of post-deploy vs baseline metrics. |
| `compose_patch.yaml` | The docker-compose fragment merged into `/opt/substrate-v2/docker-compose.yml`. |
| `patch_compose.py` | Helper that applied the YAML patch idempotently. |
| `patch_runner.py` | Helper that patched the GENA `runner.py` to wire BIJOTEL processors at startup. |
| `requirements_addition.txt` | Lines appended to `/opt/substrate-v2/requirements.txt` (OpenTelemetry + rfc8785 — pre-`[api]` extra era). |
| `runner_hook.py.snippet` | The 12-line snippet that was inserted into GENA's startup. |

## Why kept

* **Forensic provenance.** The chain on GENA was bootstrapped on
  2026-05-10 by these exact scripts; preserving them documents how the
  initial state came about. Useful if an auditor asks "where did
  span#1 come from?".
* **Reference for future deploys.** When v2.x ships and we need a new
  bootstrap onto a fresh host, these files show the working pattern.
* **Honest about superseded paths.** Day 5+ moved BIJOTEL onto a
  proper PyPI-installed wheel via the standard `requirements.txt`
  pattern. These scripts predate that and used a manual snippet
  injection — kept here as the historical record, not as the current
  install path.

## Why NOT used today

* The Day 10 + Day 12 deploys both used the simpler `pip install
  bijotel[api]` → `docker compose build` → `docker compose up -d`
  cycle. No script invocation needed.
* `runner_hook.py.snippet` is now obsolete: `bijotel` ships its own
  `init()` function that's called from GENA's runner directly.

If you're looking for the CURRENT deploy protocol, see the README
"Production validated" section + `INTEGRATION_TEST_20260523.md` Part A.
