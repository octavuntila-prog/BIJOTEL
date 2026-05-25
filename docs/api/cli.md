# CLI Reference

The `bijotel` CLI is installed when you `pip install bijotel`.

```bash
bijotel --help
```

## Subcommands

| Command | Description |
|---|---|
| `verify` | Verify HMAC chain integrity end-to-end |
| `verify-export` | Verify an exported chain JSON file |
| `inspect` | Show full canonical body for one span |
| `stats` | Summary stats: chain, CAS, policy state |
| `list` | List spans with optional filters |
| `export` | Export chain to portable signed JSON |
| `regression` | Run F12 regression detection |
| `energy backfill` | Compute Wh + gCO₂ for all existing chain rows |
| `energy summary` | Aggregate energy per model / per region |
| `serve` | Start the BIJOTEL HTTP API + dashboard |

## `verify`

```bash
bijotel verify --db chain.db
# → Chain VALID (5889 entries).
```

```bash
# Use a hex secret instead of $BIJOTEL_HMAC_SECRET env var
bijotel verify --db chain.db --secret-hex deadbeef...
```

**Exit codes**:

- `0` — chain valid
- `3` — chain broken (failure reason on stderr, with exact `seq`)

## `verify-export`

```bash
bijotel verify-export demo_chain.json --secret-hex deadbeef...
# → Export VALID: demo_chain.json
```

**Exit codes**:

- `0` — export valid
- `1` — export invalid (failure reason on stderr, with `seq`)

## `inspect`

```bash
bijotel inspect --db chain.db --seq 100
```

Prints the full canonical body of the span at seq=100, including all
`gen_ai.*` attributes, plus the CAS entry it dedups to.

## `stats`

```bash
bijotel stats --db chain.db
```

```
=== Chain ===
Total spans:    5889
  ALLOWED:      5887
  WARN:         2
  BLOCKED:      0

=== CAS ===
Unique bodies:  4521
Total refs:     5889
Dedup factor:   1.30x
```

## `list`

```bash
# Last 10 entries
bijotel list --db chain.db --limit 10

# Filter by provider
bijotel list --db chain.db --provider anthropic --limit 20

# Filter by model
bijotel list --db chain.db --model claude-haiku-4-5-20251001 --limit 50
```

## `export`

```bash
bijotel export --db chain.db --output my_export.json
# Exported chain to my_export.json
#   format:        bijotel-chain-v1
#   entries_count: 5889
#   head_hash:     02db0af4...
#   size:          56,033,175 bytes
```

The export is the portable, auditable format. Ship it to your auditor
along with the HMAC secret (out of band) and they `verify-export` it.

## `regression`

```bash
bijotel regression --db chain.db --window 100
```

Output is JSON with z-score + IQR anomalies per dimension
(input_tokens, output_tokens, cost, latency). Pipe to `jq` for
human-readable filtering.

## `energy backfill`

```bash
bijotel energy backfill --db chain.db --region us-east
```

Idempotent. Walks every `chain` row, computes Wh and gCO₂, populates
the `energy_log` table.

## `energy summary`

```bash
bijotel energy summary --db chain.db
```

```
Total: 19.95 Wh / 7.58 g CO₂ over 5459 calls

By model:
  claude-haiku-4-5-20251001:  8.21 Wh / 3.12 g CO₂ (4102 calls)
  claude-sonnet-4-20250514:  11.74 Wh / 4.46 g CO₂ (1357 calls)

By region:
  us-east: 19.95 Wh / 7.58 g CO₂ (5459 calls)
```

## `serve`

```bash
bijotel serve --port 8080 --host 0.0.0.0 --db chain.db --dashboard
```

| Flag | Default | Purpose |
|---|---|---|
| `--port` | 8080 | HTTP port |
| `--host` | 127.0.0.1 | Bind address |
| `--db` | `chain.db` | Chain SQLite path |
| `--dashboard` | off | Serve the React SPA bundle at `/` |

Auth via `BIJOTEL_API_KEY` env var (Bearer). See [Dashboard guide](../guides/dashboard.md#authentication).

## Common patterns

```bash
# Inspect what's in chain.db
bijotel stats --db chain.db && bijotel list --db chain.db --limit 5

# Cron audit
0 * * * * bijotel verify --db /data/chain.db || alert-on-call.sh

# Periodic export to S3
bijotel export --db chain.db -o /tmp/export.json && aws s3 cp /tmp/export.json s3://audits/$(date +%Y%m%d).json
```

## Next

- [REST API](rest.md) — same operations over HTTP
- [Python API](python.md) — programmatic access
