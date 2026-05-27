# BIJOTEL Architecture

This document covers the runtime call path, on-disk schema, and the
14-layer bijuterii positioning. Aimed at new contributors and at
auditors trying to convince themselves the chain integrity story is
real.

## Runtime call flow

When a host application makes an LLM call wrapped by BIJOTEL, the
following happens before the SDK reaches the network:

```mermaid
flowchart TD
    A[Host: client.messages.create]
    A --> B{PolicyEngine.evaluate}
    B -->|deny| Z[PolicyDeniedError raised<br/>synthetic span with<br/>bijotel.blocked=true]
    B -->|warn| C
    B -->|allow| C[Anthropic SDK call]

    Z --> X[HmacChainSpanProcessor]
    C --> D[AnthropicInstrumentor]
    D --> E[OTel ReadableSpan]
    E --> F[HmacChainSpanProcessor]
    E --> G[CasSpanProcessor]
    E --> H[Optional: FingerprintSpanProcessor]

    F --> I[(chain table)]
    G --> J[(cas table)]
    H --> K[(fingerprints table)]

    I --> L[bijotel verify]
    I --> M[bijotel serve<br/>FastAPI]
    J --> M
    K --> M
    M --> N[Dashboard pages]

    style B fill:#fef3c7,stroke:#f59e0b
    style F fill:#dcfce7,stroke:#16a34a
    style G fill:#dcfce7,stroke:#16a34a
    style M fill:#e0e7ff,stroke:#4f46e5
```

Key points:

* The **policy gate runs *before* the SDK** — no money spent on a
  request the policy would block.
* `deny` produces a **synthetic span** so the audit chain records the
  block (with `bijotel.blocked=true` attribute), not a silent
  refusal. The host receives `PolicyDeniedError`.
* `warn` does NOT short-circuit — the call proceeds, and the warning
  list attaches to the eventual span via `bijotel.policy.warning`.
* The host's existing `AnthropicInstrumentor` (or any OTel GenAI
  instrumentor) is the source of spans. BIJOTEL `SpanProcessor`s do
  **not** wrap the SDK call — they observe.

## Span sealing detail

```mermaid
sequenceDiagram
    participant Span as OTel ReadableSpan
    participant HMC as HmacChainSpanProcessor
    participant CAS as CasSpanProcessor
    participant DB as chain.db

    Span->>HMC: on_end(span)
    HMC->>HMC: canonical_dict = span_to_canonical_dict
    HMC->>HMC: canonical_body = JCS(canonical_dict)
    HMC->>HMC: canonical_hash = SHA-256(canonical_body)
    HMC->>DB: BEGIN IMMEDIATE
    HMC->>DB: SELECT MAX(hmac_hash) → prev_hash
    HMC->>HMC: hmac_hash = HMAC-SHA256(secret, prev_hash || canonical_hash)
    HMC->>DB: INSERT INTO chain (...)
    HMC->>DB: COMMIT

    Span->>CAS: on_end(span)
    CAS->>CAS: semantic_dict = span_to_semantic_dict
    CAS->>CAS: body_hash = SHA-256(JCS(semantic_dict))
    CAS->>DB: SELECT body_hash FROM cas
    alt body_hash exists
        CAS->>DB: UPDATE cas SET ref_count = ref_count + 1
    else not exists
        CAS->>DB: INSERT INTO cas (body_hash, body, ref_count=1)
    end
```

* **JCS** = RFC 8785 JSON Canonicalization Scheme. Two different
  serializers of the same object produce the *same byte sequence*, so
  the hash is stable across SDK versions and Python interpreters.
* **BEGIN IMMEDIATE** acquires the RESERVED lock before the SELECT,
  serializing the SELECT-then-INSERT critical section across multiple
  writer processes that share the same chain.db. The `busy_timeout`
  PRAGMA makes concurrent writers wait up to 5 seconds instead of
  raising `SQLITE_BUSY`.

## On-disk schema (chain.db)

```mermaid
erDiagram
    chain {
        INTEGER seq PK
        INTEGER timestamp_ns
        TEXT trace_id
        TEXT span_id
        TEXT span_name
        TEXT span_kind
        BLOB canonical_body
        TEXT canonical_hash
        TEXT prev_hash
        TEXT hmac_hash
        TEXT semantic_body_hash "FK to cas.body_hash"
    }
    cas {
        TEXT body_hash PK
        BLOB body
        INTEGER first_seen_ns
        INTEGER ref_count
    }
    dag_nodes {
        TEXT content_hash PK
        TEXT refs_json
        INTEGER created_ns
    }
    dag_refs {
        TEXT from_hash
        TEXT to_hash
    }
    regression_runs {
        INTEGER id PK
        INTEGER created_ns
        INTEGER window
        REAL z_threshold
        TEXT filter_model
        INTEGER total_anomalies
        TEXT status
        TEXT result_json
    }

    chain ||--o| cas : "semantic_body_hash"
    dag_nodes ||--o{ dag_refs : "from_hash"
    dag_nodes ||--o{ dag_refs : "to_hash"
```

The five tables coexist inside a single SQLite file. The chain table
is the canonical source of truth (everything else is either dedup
storage or derived); deleting `cas` / `dag_*` / `regression_runs`
doesn't break verification, only loses dedup + history.

## Verification path

```mermaid
flowchart LR
    A[bijotel verify --db chain.db] --> B[Open chain.db]
    B --> C[Iterate seq ASC]
    C --> D{Recompute<br/>SHA-256 canonical_body}
    D -->|mismatch| FAIL1[canonical_hash mismatch<br/>body mutated]
    D -->|match| E{prev_hash =<br/>previous hmac_hash?}
    E -->|no| FAIL2[chain broken]
    E -->|yes| F{Recompute<br/>HMAC prev_hash + canonical_hash}
    F -->|mismatch| FAIL3[hmac_hash mismatch<br/>secret wrong<br/>or hmac mutated]
    F -->|match| C
    C -->|EOF| OK[Chain VALID]

    style FAIL1 fill:#fee2e2,stroke:#dc2626
    style FAIL2 fill:#fee2e2,stroke:#dc2626
    style FAIL3 fill:#fee2e2,stroke:#dc2626
    style OK fill:#dcfce7,stroke:#16a34a
```

The same logic ships server-side in `POST /chain/verify` with
`full=true`. The CLI and the API give the same answer on the same
chain.db + secret.

## 14-layer bijuterii positioning

```mermaid
graph TB
    subgraph HOST["Host application"]
        UA[User-facing wrapper]
    end

    subgraph PRE["Pre-call layer"]
        L10["#10 PolicyGate<br/>8 rule factories"]
        L15["#15 Routing<br/>Pareto + Budget"]
        L18["#18 Misalignment Probes"]
    end

    subgraph CALL["Call layer"]
        L7P["#7 Provider Protocol<br/>Anthropic / OpenAI"]
        L19["#19 OTel GenAI Semconv"]
    end

    subgraph SEAL["Sealing layer"]
        L11["#11 Forensic Chain<br/>HMAC-SHA256"]
        L2A["#2 CAS<br/>semantic dedup"]
        L2B["#2 Merkle DAG"]
        L7F["#7 Fingerprint<br/>deterministic + semantic"]
        L5["#5 AST Safety<br/>Bash + Python"]
    end

    subgraph ANALYZE["Analysis layer"]
        L16["#16 Regression Detection<br/>z-score + IQR"]
        L3["#3 Energy Accounting<br/>Wh + gCO₂ per call"]
        L9["#9 Consensus Voting<br/>N-model agreement"]
        D["Combo D<br/>Containment Guard"]
    end

    UA --> L10
    UA --> L15
    L10 --> L7P
    L15 --> L7P
    L7P --> L19
    L19 --> L11
    L19 --> L2A
    L2A --> L2B
    L19 --> L7F
    L19 --> L5
    L11 --> L16
    L11 --> L3
    L11 --> L9
    L11 --> D
    L10 --> D

    style L10 fill:#dcfce7,stroke:#16a34a
    style L11 fill:#dcfce7,stroke:#16a34a
    style L2A fill:#dcfce7,stroke:#16a34a
    style L2B fill:#dbeafe,stroke:#2563eb
    style L7P fill:#dcfce7,stroke:#16a34a
    style L19 fill:#dcfce7,stroke:#16a34a
    style L16 fill:#dcfce7,stroke:#16a34a
    style L7F fill:#dbeafe,stroke:#2563eb
    style L5 fill:#dbeafe,stroke:#2563eb
    style L15 fill:#dbeafe,stroke:#2563eb
    style L18 fill:#dbeafe,stroke:#2563eb
    style D fill:#dbeafe,stroke:#2563eb
    style L3 fill:#dcfce7,stroke:#16a34a
    style L9 fill:#dcfce7,stroke:#16a34a
```

Green = active (runtime evidence present in this build, including the
GENA dual-observer deploy). Blue = available (code ships in the wheel,
host opts in via configuration). All 14 catalogued layers are now
shipped; no layer remains in the planned column.

## Deploy topologies

### Single-process

The simplest deploy: one Python process owns the chain.db. All
processors are local. Used by smoke tests and small projects.

### Multi-writer (production)

```mermaid
flowchart LR
    P1[Agent process 1] -->|HmacChainSpanProcessor| DB[(chain.db<br/>WAL mode)]
    P2[Agent process 2] -->|HmacChainSpanProcessor| DB
    P3[Agent process 3] -->|HmacChainSpanProcessor| DB
    P4[Agent process N] -->|HmacChainSpanProcessor| DB

    DB --> RD[Read-only<br/>bijotel serve]
    DB --> CLI[bijotel verify / regression]
    RD --> DASH[Dashboard]
```

The pattern used on GENA (Day-10 integration test). Each agent
container has its own `HmacChainSpanProcessor` writing into a
shared `/data/bijotel_chain.db`. The chain stays linear and valid
because every writer holds RESERVED via `BEGIN IMMEDIATE` for the
critical SELECT-prev/INSERT section. WAL mode lets readers
(``bijotel serve``, ``bijotel verify``) coexist without blocking the
writers.

### Forensic export to auditor — symmetric (v1, default)

```mermaid
sequenceDiagram
    participant Ops as Operator
    participant API as bijotel serve
    participant DB as chain.db
    participant File as audit_TS.json
    participant Aud as Auditor (offline)

    Ops->>API: POST /export
    API->>DB: SELECT * FROM chain ORDER BY seq
    API->>API: build bijotel-chain-v1 envelope
    API->>API: chain_signature = HMAC(secret, head_hash + count)
    API->>File: signed JSON
    File->>Aud: (out-of-band — email / S3)
    Aud->>Aud: POST /export/verify (own server, same secret)
    Aud->>Aud: per-entry HMAC recompute
    Aud->>Aud: chain_signature match check
    Note over Aud: valid=true, entries_count=4950
```

The auditor verifies with the **shared HMAC secret**. That secret has
to be transmitted out-of-band, and the auditor who holds it can also
*forge* valid chains. This is the trust limitation v2.1.0 was built
to close.

### Forensic export with Ed25519 attestation (v2.1.0+)

```mermaid
sequenceDiagram
    participant Ops as Operator
    participant API as bijotel export --sign-key
    participant DB as chain.db
    participant File as audit_TS.json (v2)
    participant Aud as Auditor (public key only)

    Ops->>API: bijotel keygen → priv.pem + pub.pem
    Note over Ops: priv.pem stays operator-side<br/>pub.pem distributed to auditor
    Ops->>API: export --sign-key priv.pem
    API->>DB: SELECT * FROM chain
    API->>API: chain_signature = HMAC(secret, ...)
    API->>API: ed25519_sig = sign(chain_signature, priv.pem)
    API->>File: bijotel-chain-v2 envelope
    File->>Aud: (out-of-band: export.json + pub.pem)
    Aud->>Aud: verify-export --public-key pub.pem
    Aud->>Aud: Ed25519 verify(chain_signature, sig, pub)
    Aud->>Aud: per-entry body_hash + chain_link checks
    Note over Aud: VALID — auditor never held the HMAC secret
```

Auditor mode requirements (v2.1.0+):

- Operator holds the HMAC secret AND the Ed25519 *private* key.
- Auditor receives the export JSON AND the Ed25519 *public* key.
- Auditor cannot forge entries — they hold only verification material.
- Cross-architecture portability: a v2 export signed on x86_64
  verifies bit-identically on aarch64. Empirically confirmed
  2026-05-26 (GENA Nuremberg → ARA Helsinki, 6,341 entries).

### Chain segmentation and archival (v2.2.0+)

At scale the active `chain.db` is peeled into archive segments while
the boundary invariant `archive.last_hmac_hash ==
next_segment.first.prev_hash` proves no entries went missing.

```mermaid
flowchart LR
    subgraph T0["Day 0 — single chain"]
        C0["chain.db<br/>seq 1..6332<br/>82 MB"]
    end
    subgraph T1["After bijotel archive --before 2026-05-20"]
        A1["archive_may10-may20.db<br/>seq 1..3783<br/>42 MB"]
        C1["chain.db<br/>seq 3784..6332<br/>40 MB"]
        A1 -. "last_hmac_hash == first.prev_hash" .-> C1
    end
    subgraph T2["Verify continuity"]
        VC["bijotel verify-continuity<br/>archive.db chain.db"]
        VC --> R["VALID, CONTINUOUS<br/>6332 entries / 2 segments"]
    end
    C0 ==> A1
    C0 ==> C1
```

Range-aware verify and export also live in v2.2.0:

- `bijotel verify --range 5000:6000` / `--since 2026-05-20` /
  `--until 2026-05-25` / `--last 1000`
- `bijotel export ... --range A:B` produces a `segment` block with
  `boundary_prev_hash` that the auditor anchors against, instead of
  GENESIS, when the slice doesn't start at seq=1.

See `docs/operations/chain-archival.md` for the operational playbook.

## Compatibility notes

* Python 3.11+ (uses `|` for union types, `tomllib`, etc.).
* OTel SDK 1.27.0+ for stable `gen_ai.*` attribute names.
* SQLite ≥ 3.35.0 for the `BEGIN IMMEDIATE` + WAL combo used by the
  hardening fix in v0.6.1.
* Tested OS: Linux (GENA production), macOS, Windows. Windows-specific
  caveats documented in README "Known issues".

---

For schema changes, see migration notes in the
[Changelog](changelog.md). For production integration evidence,
see the GENA + ARA chain stats reported in the
[Threat Model](threat-model.md) and the per-release notes on
[GitHub Releases](https://github.com/octavuntila-prog/BIJOTEL/releases).
