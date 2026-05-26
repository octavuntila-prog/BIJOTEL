# BIJOTEL OTel Collector Exporter — Design

**Status:** Design only. No code yet. This document is the spec for a future Go implementation.

**Repo (planned):** `github.com/octavuntila-prog/bijotel-collector`

**Owner:** Aisophical SRL.

**Last revised:** 2026-05-26

---

## 1. Problem statement

The current BIJOTEL ships as a Python package (`pip install bijotel`) that
inserts itself into an application via an OpenTelemetry SDK
`SpanProcessor`. That works well for greenfield code, but it imposes two
hard constraints that block adoption in larger orgs:

1. **Application-code access.** Many enterprise teams cannot modify
   the apps they audit. The audit is bolted on by the platform team,
   not by the model-using team. A Python `pip install` plus
   `provider.add_span_processor(HmacChainSpanProcessor(...))` is a
   code change those platform teams can't merge into someone else's
   service.
2. **Polyglot fleets.** Production gateways for LLM traffic (Kong AI
   Gateway, Bifrost, Portkey, Tyk AI, LiteLLM proxy, OpenLLMetry-style
   sidecars) are written in Go, Rust, or TypeScript. They already
   emit OpenTelemetry traces with `gen_ai.*` attributes per the
   semconv. A Python-only sealer can't sit in that path without
   adding a Python hop.

The goal of the BIJOTEL OTel Collector exporter is to **receive OTLP
spans from any source and seal them into a BIJOTEL HMAC chain without
the source application knowing anything about BIJOTEL.**

The exporter is a sidecar / gateway component. The application only
needs to point its existing OTLP exporter at the collector's endpoint.
Everything downstream (canonicalization, HMAC chaining, SQLite write,
Ed25519 signing) happens server-side.

---

## 2. Architecture

```
+----------------------+      OTLP/gRPC :4317      +-----------------------+
|  Any application     | -----------------------> |  OTel Collector       |
|  (Python, Go, Node,  |      OTLP/HTTP :4318     |   + filterprocessor   |
|   JVM, Rust, ...)    | -----------------------> |   + bijotelexporter   |
+----------------------+                           +-----------------------+
                                                            |
                                                            v
                                              +--------------------------+
                                              |  chain.db  (SQLite WAL)  |
                                              |  + Ed25519 signed exports|
                                              |  + /healthz + /metrics   |
                                              +--------------------------+
                                                            ^
                                                            |
                                                (Python bijotel CLI reads
                                                 + verifies + audits)
```

The exporter is packaged as a Go plugin for
[opentelemetry-collector-contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib).
It can run in three modes (see §5).

The key architectural commitment: **the Go side writes; the Python side
reads, verifies, and audits**. They never share a process. The contract
between them is the `chain.db` SQLite file layout — byte-identical to
what the Python `HmacChainSpanProcessor` writes today.

---

## 3. Exporter design

### 3.1. Receiver

Standard OTel Collector OTLP receiver:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318
```

No custom receiver — we ride on what's already there.

### 3.2. Filter processor

`filterprocessor` from collector-contrib drops everything that isn't
GenAI traffic, so the chain stays focused:

```yaml
processors:
  filter/genai:
    error_mode: ignore
    traces:
      span:
        - 'attributes["gen_ai.request.model"] == nil and attributes["gen_ai.system"] == nil'
```

Spans without `gen_ai.*` attributes fall through to whatever other
exporters are wired (Jaeger, Tempo, etc.). Only GenAI spans hit the
BIJOTEL exporter.

### 3.3. The exporter itself

`bijotelexporter` is the new component. Pseudo-flow per span:

```
for each pdata.Span:
    span_dict := build_canonical_dict(span)            // step A
    body_bytes := jcs.Marshal(span_dict)               // step B (RFC 8785)
    canonical_hash := sha256(body_bytes)               // step C
    prev_hash := db.last_hmac_hash()                   // step D
    hmac_hash := hmac_sha256(secret,
                             prev_hash || canonical_hash)   // step E
    db.insert_chain_row(                                // step F
        seq            = prev.seq + 1,
        timestamp_ns   = span.start_time,
        prev_hash      = prev_hash,
        canonical_body = body_bytes,
        canonical_hash = canonical_hash,
        hmac_hash      = hmac_hash,
    )
    if ed25519_key != nil and sign_each_n_rows >= 1:    // step G (optional)
        db.insert_signed_segment(...)
```

Every numbered step has a one-to-one counterpart in the Python
`HmacChainSpanProcessor.on_end()`. We are not reinventing — we are
porting.

### 3.4. Config

```yaml
exporters:
  bijotel:
    db_path: /data/chain.db
    hmac_secret_env: BIJOTEL_HMAC_SECRET   # 32-byte hex, never inline
    sign_key_path: /keys/bijotel_ed25519.pem  # optional, enables signed exports
    filter_prefix: "gen_ai."               # only canonicalize these attrs
    canonical_dict_version: "1.41"         # OTel GenAI semconv version
    batch_size: 100                        # WAL group-commit
    batch_timeout_ms: 250
    healthz_addr: 0.0.0.0:9090
    metrics_addr: 0.0.0.0:9091
```

The `canonical_dict_version` field is critical: Python BIJOTEL v2.4.0+
ships with `v1.41` semconv support; the Go exporter has to track the
same versioning so a chain written by Go-v1.41 verifies with
Python-v1.41 (see §4).

---

## 4. Cross-compatibility guarantee

This is the core promise. **A `chain.db` written by Go must verify
under `bijotel verify` written in Python, with no migration step.**

Concretely:

| Concern               | Python (today)                                     | Go (planned)                                   |
|-----------------------|-----------------------------------------------------|------------------------------------------------|
| Schema (SQLite)       | `chain(seq, timestamp_ns, prev_hash, canonical_body, canonical_hash, hmac_hash)` | identical |
| HMAC formula          | `HMAC-SHA256(secret, prev_hash ‖ canonical_hash)` | identical |
| Canonical hash        | `SHA-256(JCS(canonical_body))`                     | identical |
| Canonical body        | JCS RFC 8785 of `canonical_dict_v1_41(span)`       | same dict shape, same JCS |
| `canonical_body` blob | `BLOB`, UTF-8 JSON                                  | identical |
| Ed25519 signed export | format v2 JSON (see `processors/export.py`)        | identical |
| Empty chain head      | `prev_hash = "0" * 64`                              | identical |
| Concurrency           | `BEGIN IMMEDIATE` + WAL                             | identical |

Verification path stays in Python: `bijotel verify --db chain.db`,
`bijotel verify-export audit.json`, `bijotel verify-continuity ...`.
No Go-side verifier ships in v0.1.

The Python CLI is the auditor of record. That separation is on
purpose: the trust anchor (verify) is fewer than 200 LOC of Python
that anyone can audit, while the writer (Go exporter, ~2000 LOC) can
ship as a binary without compromising trust. If the auditor ever
trusted itself — the writer — the property would be lost.

### 4.1. JCS in Go

There is no canonical RFC 8785 Go library with the same backing as
the Python `rfc8785` package. Options, in increasing risk order:

1. **Port a JCS subset by hand.** Sort map keys lexicographically,
   serialize numbers with the minimal-form ECMA-262 rules,
   UTF-8 strings, no insignificant whitespace. ~150 LOC. Easy to
   review. **Recommended for v0.1.**
2. **Use `github.com/nicholasgasior/gojcs`** — small, focused, but
   not battle-tested. Pin a hash, vendor the source.
3. **Use `cyberphone/json-canonicalization`** — has Go bindings,
   reference impl, but heavier and AGPL on some files. Audit the
   license tree before pulling in.

For each option, the acceptance test is the same: hand-craft 200
GenAI spans, run the Python `rfc8785` and the Go implementation on
the same JSON, compare bytes. Bytes must match.

---

## 5. Deployment modes

### 5.1. Sidecar (Kubernetes)

```
Pod
├── app-container          (any LLM-using service)
│   └── OTLP exporter → localhost:4317
└── bijotel-collector      (this exporter)
    ├── OTLP receiver :4317
    ├── filter/genai
    ├── bijotelexporter → /data/chain.db
    └── volumeMount: /data (PVC, RWO)
```

The app emits OTLP to localhost; the sidecar writes to a per-pod PVC.
One pod = one chain. Aggregation is offline (rsync chains nightly,
verify-continuity).

**Pros:** isolated, no network hops, no shared-state contention.
**Cons:** one chain per replica → N chains for N replicas; aggregation
work pushed to operators.

### 5.2. Gateway (centralized)

```
+-----+     +-----+     +-----+
| app | --> | app | --> | app |    (N services)
+--+--+     +--+--+     +--+--+
   |           |           |
   +-----------+-----------+
               |
               v
   +---------------------+
   | central collector   |
   |  bijotel exporter   |
   +---------------------+
               |
               v
       /data/chain.db
       (multi-tenant via
        resource attrs)
```

All services point OTLP at a central BIJOTEL collector. One chain
covers the whole fleet. Tenant isolation by `service.namespace` /
`service.name` resource attribute, surfaced as a chain column
(`tenant_id`) in v0.2+.

**Pros:** one chain to verify, one place to back up, easy auditing.
**Cons:** SPOF for the audit trail (mitigated by WAL + offsite sync),
write contention at >5k spans/sec (mitigated by per-tenant chain
sharding in v0.3+).

### 5.3. Standalone binary

```
$ bijotel-collector --config collector.yaml
```

No OTel Collector framework — just an OTLP gRPC/HTTP server + the
exporter logic in one binary. Useful for embedded / edge / on-prem
shops that can't run the full collector. **Ships in v0.3.**

---

## 6. Build system

| Concern         | Choice                                                         |
|-----------------|----------------------------------------------------------------|
| Language        | Go 1.22+                                                       |
| Repo            | `github.com/octavuntila-prog/bijotel-collector`                |
| Framework       | `go.opentelemetry.io/collector` + `collector-contrib`          |
| SQLite          | `modernc.org/sqlite` (pure Go, no CGo) — see §9 trade-off      |
| Crypto          | `crypto/hmac`, `crypto/sha256`, `crypto/ed25519` (stdlib)      |
| JCS             | in-tree port (see §4.1) — vendored, audited                    |
| CLI             | `urfave/cli/v3` for the standalone-mode binary                 |
| Logging         | `go.opentelemetry.io/collector/component` logger (zap under)   |
| Testing         | `testing` stdlib + table-driven; integration via testcontainers |
| Lint            | `golangci-lint` with collector-contrib's `.golangci.yml`       |
| CI              | GitHub Actions — Go test + lint + build matrix linux/macOS/win |

```
make build    # bijotel-collector binary, $(uname)/$(arch)
make docker   # OCI image, multi-arch (amd64 + arm64)
make test     # unit + integration
make verify   # builds image, writes 1000-span chain, then runs
              # the Python bijotel verify against the SQLite file
              # — the byte-level cross-compat gate
```

---

## 7. Testing strategy

1. **Unit (Go side):**
   - HMAC chain math: same secret + same input → same `hmac_hash`
     as the Python ground-truth (table-driven fixtures lifted from
     `tests/test_processors_chain.py`).
   - JCS: 200-vector corpus from
     [rfc8785 reference vectors](https://www.rfc-editor.org/rfc/rfc8785.html#appendix-B),
     plus 50 GenAI-shaped dicts.
   - SQLite write path: `BEGIN IMMEDIATE` contention under N goroutines.
2. **Cross-language integration:**
   - Spin up the Go exporter in a container, push 1000 OTLP spans, then
     run `python -m bijotel verify --db chain.db` against the file.
     Must return `valid=True`.
   - Same but with `bijotel verify-export` against an Ed25519-signed
     export from the Go side.
3. **Load:** 1000 spans/sec sustained for 1 hour on a 2-core / 2 GiB
   pod. Target p99 write latency < 50 ms with `batch_timeout_ms=250`.
4. **Chaos:**
   - SIGKILL mid-batch: chain must remain verifiable (WAL replay).
   - Disk full: exporter returns OTLP error to caller; no partial row
     committed.
   - Clock jump backwards: spans accepted, `timestamp_ns` field reflects
     reality; chain still verifies (the chain has no per-row time
     monotonicity invariant; only `seq` is required to be monotonic).
5. **Determinism:** replay the same 100-span OTLP batch ten times into
   ten fresh DBs with the same secret — all ten DBs must be
   byte-identical (modulo timestamps if the test fixes them).

---

## 8. Milestones

| Tag      | Window    | Scope                                                          |
|----------|-----------|----------------------------------------------------------------|
| v0.1.0   | weeks 1-2 | OTLP → HMAC chain → SQLite, sidecar deployment, healthz        |
| v0.2.0   | weeks 2-3 | Ed25519 signed exports, archival (mirror Python v2.2 archive), config validation |
| v0.3.0   | weeks 3-4 | Docker images, Helm chart, standalone-binary mode, prom metrics |
| v0.4.0   | week 5    | Multi-tenant column (tenant_id), per-tenant chain sharding option |
| v1.0.0   | week 8+   | Production-ready after 30-day soak on at least one external pilot |

Each milestone ends with the cross-language gate from §7.2 — if the
Python verifier rejects, the milestone is not shipped.

---

## 9. Open questions

These are not blockers for v0.1 but need a decision before v0.2:

1. **SQLite driver — CGo vs pure Go.**
   `github.com/mattn/go-sqlite3` (CGo) is the canonical choice and is
   the fastest. But CGo blocks easy cross-compile and adds a libc
   dependency to the static binary story. `modernc.org/sqlite`
   (pure Go) sidesteps both but is roughly 2× slower on write-heavy
   workloads in the maintainer's own benchmarks. **Default: pure Go
   for v0.1**, with a build-tag fallback to CGo for shops that need
   the speed. Revisit after the load test.
2. **JCS implementation.**
   Three options in §4.1. Default: hand-port the subset, ~150 LOC,
   gate it on the 200-vector test corpus. Revisit if compatibility
   bugs appear.
3. **Multi-tenant chain layout.**
   Either one chain.db per tenant (clean isolation, more files) or
   one chain with `tenant_id` column (one file, more contention). The
   Python verifier doesn't care — it accepts both. Default: one chain
   per tenant for v0.2, add the column option in v0.4 once we have
   real-world tenant counts.
4. **Versioning the canonical-dict.**
   Python BIJOTEL bumps the canonical-dict version every minor release
   (v0.48b → v1.41). The Go exporter has to track. Concrete answer:
   ship the canonical-dict shape as a versioned JSON schema file under
   `bijotel-collector/schema/v1_41.json` and re-use the same file in
   the Python project so the two stay literally byte-aligned. Adopt
   this in v0.2.
5. **gRPC keep-alive defaults.**
   Collector defaults are fine for in-cluster; for cross-region edge
   we'll want explicit `keepalive.MinTime = 10s`. Defer to v0.3.

---

## 10. Out of scope (v0.x)

- **Verification on the Go side.** Verification stays in Python. The
  separation is a feature.
- **Rotation of HMAC secrets at runtime.** Restart the exporter with
  a new env var. Online rotation is a Python-side concern (see
  `docs/operations/secret-rotation.md`).
- **Web UI.** The dashboard is Python-served by `bijotel serve`. The
  Go exporter does not ship UI.
- **PII redaction.** The exporter writes whatever the collector pipeline
  hands it. If you need redaction, put a `transformprocessor` in front
  of `bijotelexporter`.
- **Anomaly detection (F12 regression).** Pure Python, runs against
  the chain post-write. Not duplicated in Go.

---

## 11. Appendix — relationship to Python BIJOTEL

| Python (this repo)                        | Go (planned)                              |
|-------------------------------------------|-------------------------------------------|
| `HmacChainSpanProcessor` (writer)         | `bijotelexporter` (writer)                |
| `verify_chain`                            | — (use Python)                            |
| `verify_export`                           | — (use Python)                            |
| `verify_continuity`                       | — (use Python)                            |
| `archive_chain`                           | mirror in v0.2                            |
| `export_chain` (signed v2 JSON)           | mirror in v0.2                            |
| `bijotel serve` (FastAPI + dashboard)     | — (Python only; the Go side is headless)  |
| `bijotel inspect / list / stats`          | — (use Python against the same chain.db)  |

The mental model: **Python is the operator's product. Go is the
deployment shim that lets the operator's product see traffic from
applications they cannot modify.**
