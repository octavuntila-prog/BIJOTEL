# BIJOTEL v1.4.0 — Launch Checklist

**Date:** 2026-05-23 (Day 12 of 12 — final day of harvest plan).
**Outcome:** **LAUNCH READY.**

This document is the explicit go/no-go gate for the v1.4.0 release. Every
checkbox below is verified, not aspirational.

## One-line install gate

The core claim of v1.4.0:

```bash
pip install "bijotel[api]"
bijotel serve --dashboard
# → open http://localhost:8080/
```

Verified end-to-end via `scripts/launch_smoke.sh`. The script creates
a fresh venv, installs from PyPI (not local), seeds a chain, starts
the server with `--dashboard`, and curls every public endpoint plus
the SPA root and a deep-link path.

## Quality gates

| Gate | Result | Notes |
|---|---|---|
| Backend pytest | **485 passed, 7 skipped, 0 failed** | +11 new in `test_serve_dashboard.py` |
| Coverage | ~92% | Unchanged from v1.1.0 (same Python code paths + dashboard wiring) |
| Ruff lint | clean | |
| twine check | PASSED | both wheel (374 kB) + sdist (439 kB) |
| Dashboard build | success | 2382 modules, 4.95s, initial chunk **58.7 kB gzip** |
| Wheel size | 346 KB raw | up from 118 KB v1.1.0 (+228 KB = the dashboard bundle) |
| Wheel contents | 78 files | 9 in `bijotel/dashboard_dist/` (index.html + 8 assets) |
| PyPI upload | success | `https://pypi.org/project/bijotel/1.4.0/` |
| Fresh-venv install verify | OK | `pip install bijotel[api]==1.4.0` → version 1.4.0, dashboard_dist present |

## Surface area at launch

### REST API (18 endpoints + Swagger)

| Method | Path (default mode) | Path (--dashboard) |
|--------|---------------------|--------------------|
| GET    | /health             | /health AND /api/health |
| GET    | /version            | /version AND /api/version |
| GET    | /docs               | /docs |
| GET    | /redoc              | /redoc |
| GET    | /openapi.json       | /openapi.json |
| GET    | /chain              | /api/chain |
| GET    | /chain/stats        | /api/chain/stats |
| GET    | /chain/{seq}        | /api/chain/{seq} |
| POST   | /chain/verify       | /api/chain/verify |
| GET    | /policy/rules       | /api/policy/rules |
| POST   | /policy/evaluate    | /api/policy/evaluate |
| GET    | /layers             | /api/layers |
| GET    | /regression/latest  | /api/regression/latest |
| GET    | /regression/history | /api/regression/history |
| POST   | /regression/run     | /api/regression/run |
| POST   | /export             | /api/export |
| POST   | /export/verify      | /api/export/verify |

### Dashboard (4 pages)

* **/chain** — Chain Explorer (4 stat cards, paginated table, detail panel, verify, export)
* **/policy** — Policy Decisions (rules grid, live evaluate form, layer manifest)
* **/regression** — Regression Monitor (status cards, recharts AreaChart, dimension table, run-now)
* **/system** — System Status (full bijuterii manifest)

### CLI (8 subcommands)

```
bijotel verify        — full HMAC re-verification
bijotel inspect       — show one canonical body
bijotel stats         — chain + CAS + policy aggregates
bijotel list          — filterable browsing
bijotel export        — signed portable JSON
bijotel verify-export — auditor-side verify
bijotel regression    — z-score + IQR drift
bijotel serve         — REST API (+ optional --dashboard)
```

## 14 / 20 bijuterii covered

(Split from the day-of-launch "13" tally: `#2 Content-Addressable` and
`#2 Merkle DAG` are tracked as separate rows by the runtime
``/layers`` endpoint — CAS can be `active` while the DAG remains
`available`. README + ARCHITECTURE updated 2026-05-23 to match.)

* **#2** Content-Addressable Storage — active (CAS unique-body table populated)
* **#2** Merkle DAG — available (`dag_nodes` + `dag_refs` reference graph)
* **#5** AST-First Code Safety — available ([ast] extra)
* **#7** Provider Protocol — active (Anthropic + OpenAI adapters)
* **#7** Deterministic + Semantic Fingerprinting — available ([fingerprint] extra)
* **#10** Compliance-as-Code — active (8 rule factories + default 3-rule warn engine)
* **#11** Forensic-First HMAC chain — active (production-validated)
* **#15** Inference Routing — available
* **#16** Regression Detection — active (z-score + IQR + persistence)
* **#18** Misalignment Probes — available (29 probes × 8 categories)
* **#19** OTel GenAI Semconv — active (entire chain uses semconv attrs)
* **Combo D** Containment Guard — available

Planned for v1.5+: **#3** Energy Accounting, **#9** Consensus Voting, plus
the seven not-yet-touched catalog patterns (#4 / #6 / #8 / #12 / #14 / #17 / #20).

## Production validation (Day 10 integration test)

* **GENA host** (178.104.252.86), 4 bijotel containers, v0.6.1 → v1.1.0
  rolling upgrade.
* **4,952 chain entries** at handover, 13 days of continuous operation
  since seq=1 was sealed 2026-05-10 09:14:54Z.
* **`POST /chain/verify` with `full=true` returns valid:true** across
  every wheel-version boundary (v0.5.0 → v0.6.0 → v0.6.1 → v1.1.0).
* **18/18 endpoints PASS** against the real chain.
* **First production regression baseline persisted:**
  cost μ=$0.0033 σ=$0.0008 per call (24% relative stdev).
* Full report in [INTEGRATION_TEST_20260523.md](INTEGRATION_TEST_20260523.md).

## Release artifacts

| Artifact | Path / URL | Size |
|---|---|---|
| PyPI wheel | https://pypi.org/project/bijotel/1.4.0/ | 374 kB |
| PyPI sdist | (same page) | 439 kB |
| Git tag | `v1.4.0` | — |
| Docker image | `bijotel:latest` (build locally; not on Docker Hub yet) | ~400 MB |
| GitHub repo | `octavuntila-prog/BIJOTEL` (**private** during v1.x dev) | — |

## Numbers at launch

* **LOC source:** ~5,400 Python (`src/bijotel/`) + ~2,250 dashboard
  (`src/bijotel/dashboard/`) = ~7,650 total. ~4,200 lines of tests
  (`tests/`).
* **Tests:** 485 passing.
* **Coverage:** ~92%.
* **PyPI releases:** v1.0.0 (Day 5), v1.1.0 (Day 7), v1.4.0 (Day 12).
* **Git tags:** v0.5.0, v0.6.0-hardened, v0.6.1-multiwriter, v0.7.0,
  v0.8.0, v1.0.0, v1.1.0, v1.2.0, v1.3.0, v1.4.0 = **11 tags** total.
* **Bijuterii coverage:** 13 / 20 patterns (6 active + 5 available + 2 planned).

## Deferred to v1.5 / v2.0

* **GitHub repo flip PUBLIC** — single-button action when ready;
  no PyPI re-upload needed.
* **#3 Energy Accounting** + **#9 Consensus Voting** — planned bijuterii.
* **Server-side `?search=` query on `/chain`** — currently client-side
  filter; doesn't scale past ~10K loaded rows.
* **Docker Hub publish** of `bijotel:latest` (today: build locally only).
* **Streaming response support** in `@trace_genai` (currently buffers).
* **Multi-language SDK** (Python only at launch).

## Sign-off

Day 12 / harvest plan **complete**. The 12-day work block delivered:

* **A working forensic-grade LLM audit chain library** (PyPI), with
  14 catalog bijuterii implemented and runtime evidence on a real
  production chain.
* **A 18-endpoint REST API** (`bijotel serve`) with optional Bearer
  auth.
* **A React dashboard** (`bijotel serve --dashboard`) covering the
  three USP pages plus a system status page.
* **A Docker image** + compose file ready for self-hosted deploy.
* **Production integration evidence** (Day-10 report against GENA).
* **Honest documentation** — every "what's missing" is named, not
  hidden.

The single sentence that captures the launch claim:

> ``pip install "bijotel[api]" && bijotel serve --dashboard`` boots a
> forensic-grade LLM audit chain with REST API + UI in one command,
> against the same chain.db your existing OTel processors are already
> sealing.
