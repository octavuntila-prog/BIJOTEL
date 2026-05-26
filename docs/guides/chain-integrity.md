# Chain Integrity Monitor

**Available from v2.8.0.**

BIJOTEL has always had two ways to look at chain trust:

1. **`bijotel verify`** — point-in-time cryptographic verification (does
   every row's HMAC still validate?).
2. **F12 regression** — payload drift detection (are token counts,
   costs, latencies behaving normally over the last N spans?).

v2.8.0 adds a third: **continuous structural monitoring of the chain
itself**. Different layer entirely.

| Tool | Watches | Reads | Trigger |
|---|---|---|---|
| `bijotel verify` | every row's HMAC | full chain | on-demand audit |
| F12 regression | payload (tokens, cost, latency) | span attributes | every N min |
| **integrity** | structural patterns (gaps, dups, bursts) | chain metadata | every N min |

F12 answers "is the model degrading?" Integrity answers "is the chain
behaving abnormally?" The two are orthogonal.

## What it detects

Six structural anomaly categories, all derived from chain rows
themselves (no payload reading required):

### 1. Sequence gaps

A jump in `chain.seq` larger than 1. Almost always means: crashed
writer (span dropped), manual `DELETE FROM chain`, or file truncation.
None of those should ever happen.

```
WARN seq 6390 -> 6392 (1 missing)
```

### 2. Timestamp anomalies

- **Backward**: row N+1 has a smaller `timestamp_ns` than row N.
  Clock jumped, or rows were inserted out of order.
- **Large gap**: > 1h between consecutive entries on a chain that
  normally records 20/h. Likely an outage.
- **Burst**: more than 10 entries in a single wall-clock second.
  Possible replay attack or runaway script.

### 3. Hash anomalies

Duplicate `canonical_hash`. The CAS dedup layer is supposed to prevent
this — if it fires, either CAS was bypassed or someone edited the DB.

### 4. Provider distribution shift

Split the window in half. If any provider's share moves by more than
20 percentage points between the two halves, flag it. Useful for
catching silent gateway failover, traffic routing changes, or a model
disappearing from the rotation.

```
anthropic: 95.0% -> 50.0% (-45.0pp)
xai:        5.0% -> 50.0% (+45.0pp)
```

### 5. Rate anomalies

First-half hourly rate vs second-half. Flag when the ratio is outside
`[0.5, 1.5]` × baseline. Catches "the digestor went down at 2pm"
without needing an out-of-band metric.

### 6. Rotation boundaries

Placeholder in v2.8.0 — detecting "this row was sealed under a
different HMAC secret" requires a candidate key set, which is operator
state. Operators who need this run `bijotel verify-continuity` with
the old + new secret across segment boundaries.

## CLI

```bash
$ bijotel integrity --db chain.db
Chain integrity: CLEAN (last 100 entries, seq 6296..6395)
------------------------------------------------------------
Sequence: no gaps
Timestamps: no anomalies
Hashes: no duplicates
Provider distribution: stable
Rate: stable
```

With anomalies:

```bash
$ bijotel integrity --db chain.db
Chain integrity: 2 ANOMALIES (last 100 entries, seq 6296..6395)
------------------------------------------------------------
Sequence: 1 gap(s)
  WARN  seq 6390 -> 6392 (1 missing)
Timestamps: no anomalies
Hashes: no duplicates
Provider distribution: stable
Rate: 19.2/h -> 8.4/h (-56.3%)
```

JSON for cron / pipelines:

```bash
$ bijotel integrity --db chain.db --json
{"window":100,"first_seq":6296,...,"clean":false,"anomaly_count":2,...}
```

Exit codes:

- `0` — clean
- `1` — anomalies detected
- `2` — DB missing or arg error

## REST API

```bash
$ curl http://localhost:8080/integrity?window=500
{
  "window": 500,
  "first_seq": 5896,
  "last_seq": 6395,
  "clean": true,
  "anomaly_count": 0,
  "sequence_gaps": [],
  "timestamp_anomalies": [],
  "hash_anomalies": [],
  "provider_shift": null,
  "rate_anomalies": [],
  "rotation_boundaries": []
}
```

Anomalies return **HTTP 200** with `clean: false` and the populated
arrays. HTTP 4xx/5xx is reserved for "analysis itself could not run"
(503 when the DB is missing).

Window range: 2 ≤ window ≤ 10000.

## Python API

```python
from bijotel.integrity import analyze_chain_integrity

report = analyze_chain_integrity("chain.db", window=200)

if not report.clean:
    print(f"{report.anomaly_count} anomalies in seq "
          f"{report.first_seq}..{report.last_seq}")
    for gap in report.sequence_gaps:
        print(f"  gap: {gap.after_seq} -> {gap.before_seq} "
              f"({gap.missing_count} missing)")
```

The class form gives you control over threshold tuning:

```python
from bijotel.integrity import ChainIntegrityMonitor
from bijotel.integrity.monitor import BURST_THRESHOLD

# Subclass if you need different thresholds for a particular
# deployment — most operators just use the convenience function.
monitor = ChainIntegrityMonitor("chain.db", window=500)
report = monitor.analyze()
```

## Cron integration

Drop into your existing BIJOTEL cron alongside F12 regression:

```
# Every 30 minutes — chain integrity
*/30 * * * * docker exec gena-v3-atelier-1 bijotel integrity \
    --db /data/bijotel_chain.db --json \
    >> /var/log/bijotel/integrity.log 2>&1
```

The `--json` flag prints one compact line per run, perfect for grep /
`jq` parsing later. The non-zero exit code on anomaly lets you wire
this into a notification pipeline:

```bash
bijotel integrity --db /data/chain.db --json > /tmp/last.json
[ $? -ne 0 ] && curl -X POST $WEBHOOK_URL -d @/tmp/last.json
```

## Tuning thresholds

The defaults are deliberately conservative — false positives in a
monitoring loop are painful. The constants live near the top of
`src/bijotel/integrity/monitor.py`:

| Constant | Default | Description |
|---|---:|---|
| `BURST_THRESHOLD` | 10 | Rows per second above which "burst" fires |
| `LARGE_GAP_SEC` | 3600 | Inter-row pause flagging as a large_gap |
| `PROVIDER_SHIFT_PCT` | 20.0 | Provider-share movement, in pp |
| `RATE_TOL` | 0.5 | First-half-vs-second-half rate ratio tolerance |
| `MIN_SPLIT_SIZE` | 10 | Minimum half-window for provider/rate checks |

If your traffic is naturally burstier than 10/sec, raise
`BURST_THRESHOLD`. If your chain normally idles for hours, raise
`LARGE_GAP_SEC`. Don't lower the thresholds blindly — every false
positive is a missed signal next time.

## What it does *not* do

- **Does not verify HMACs.** That is `bijotel verify`. Integrity reads
  the canonical_hash and prev_hash as opaque tokens — it checks for
  *patterns*, not cryptographic validity.
- **Does not read span payloads.** That is F12 regression. Integrity
  pulls `canonical_body` only to extract the provider name; everything
  else is metadata.
- **Does not modify the chain.** Pure read.

## Public API

```python
from bijotel.integrity import (
    ChainIntegrityMonitor,
    IntegrityReport,
    analyze_chain_integrity,
)
```

Each is re-exported from `bijotel` directly.
