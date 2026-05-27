# Cross-Ecosystem View

When you run BIJOTEL on more than one ecosystem (e.g. **GENA** + **ARA**,
or production + staging), you need a single place to see totals,
provider distribution, and per-chain health without flipping between
dashboards. `CrossEcosystemView` (v2.13.0) does exactly that — read-only,
no chain merging, each chain stays sovereign.

## Quick start

```python
from bijotel.cross_view import CrossEcosystemView

view = CrossEcosystemView()
view.add_chain("GENA", db_path="/data/bijotel_chain.db")
view.add_chain("ARA",  db_path="/app/data/bijotel_chain.db")

print(view.summary())
```

Output (abbreviated):

```json
{
  "ecosystems": 2,
  "total_entries": 7023,
  "total_providers": ["anthropic", "openai", "xai"],
  "earliest_timestamp_ns": 1715313600000000000,
  "latest_timestamp_ns":  1717804800000000000,
  "per_ecosystem": {
    "GENA": {"entries": 6805, "providers": ["anthropic", "xai"], ...},
    "ARA":  {"entries": 218,  "providers": ["anthropic", "openai"], ...}
  }
}
```

## CLI

Equivalent on the command line:

```bash
bijotel cross-view \
  --chain "GENA=/data/bijotel_chain.db" \
  --chain "ARA=/app/data/bijotel_chain.db" \
  --json
```

Pass each chain as `name=path`. The path can be a `chain.db` (SQLite)
or a pre-exported JSON file — the loader picks based on suffix.

Add `--integrity` to also run per-chain integrity checks:

```bash
bijotel cross-view \
  --chain "GENA=/data/bijotel_chain.db" \
  --integrity
```

Without HMAC secrets, the integrity check is **structural only** (row
count + that `canonical_body` parses). Pass `hmac_secrets` to
`view.integrity_report({"GENA": secret_bytes, ...})` in Python for the
full HMAC verify.

## Mixed sources

DB and export JSON in the same view:

```python
view.add_chain("GENA",       db_path="/data/bijotel_chain.db")
view.add_chain("ARA_remote", export_path="/tmp/ara_chain.json")
```

This is the workflow for auditing a remote chain from your laptop —
the remote operator exports their chain to JSON, hands it to you, and
you fold it into your local view.

## What it proves

- Combined entry count + provider union across N ecosystems
- Timeline overlap detection (do any two chains have entries in the
  same time window?)
- Shared provider set (which providers appear in multiple chains?)
- Per-chain integrity (structural check + HMAC when secret provided)

## What it does NOT prove

- It does **not** merge chains. Each chain keeps its own HMAC secret,
  Ed25519 keypair, and Rekor anchor (if any).
- It does **not** prove cross-chain causality. Two chains showing the
  same provider at the same time means just that — no claim about
  whether they share data or trust.
- It does **not** modify any chain.

## Use cases

| Scenario | What you get |
|---|---|
| Daily ops dashboard for a 2+ ecosystem deployment | `view.summary()` totals |
| Auditor reviewing your stack | full per-ecosystem breakdown |
| Comparing pre/post deploy of a change | two snapshots of the same chain |
| Cross-team operator sharing | export JSON + auditor uses `add_chain(export_path=...)` |

## See also

- [Ecosystem Bootstrap](../operations/ecosystem-bootstrap.md) — how to
  wire BIJOTEL on a new ecosystem
- [Chain Archival](../operations/chain-archival.md) — how to produce
  the export JSONs that `cross_view` can consume
