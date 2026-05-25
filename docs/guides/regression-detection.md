# Regression Detection

## Overview

F12 (`bijotel regression`) watches your chain for statistical drift.
It catches:

- Model degradation (responses suddenly shorter / longer / more expensive)
- Provider config changes (silent model swaps, token-counting changes)
- Cost anomalies (a rogue agent burning budget overnight)
- Token-usage drift (prompt template change cascading through downstream)

Without making LLM calls — it operates **on your existing chain data**.

## CLI

```bash
# One-shot regression check on the last 100 entries
bijotel regression --db chain.db --window 100

# Set thresholds
bijotel regression --db chain.db --window 100 --z-threshold 3.0
```

Output is a JSON-friendly summary: dimensions checked, anomaly counts,
sample seqs.

## Cron pattern

```cron
# Hourly regression sweep, log to file
0 * * * * /usr/local/bin/bijotel regression --db /data/chain.db --window 200 >> /var/log/bijotel/regression.log 2>&1
```

GENA runs this hourly in production.

## REST API

```bash
# Latest result (cached, fast)
curl http://localhost:8080/api/regression/latest

# History
curl http://localhost:8080/api/regression/history?limit=50

# Run on demand
curl -X POST http://localhost:8080/api/regression/run \
  -H "Content-Type: application/json" \
  -d '{"window": 100}'
```

## Dimensions tracked

| Dimension | What drift indicates |
|---|---|
| `input_tokens` | Prompt template change |
| `output_tokens` | Model verbosity drift, truncation, refusal patterns |
| `cost_usd` | Provider price change, model swap, runaway agent |
| `latency_ms` | Provider degradation, network issues |

## Detection methods

Two statistical tests, both reported:

- **Z-score**: anomaly if `|z| > 3.0` (configurable). Sensitive to
  outliers, fast.
- **IQR**: anomaly if value beyond `Q1 - 1.5·IQR` or `Q3 + 1.5·IQR`.
  Robust to a few extreme outliers.

A dimension is flagged when **either** method fires.

## Reading the results

```json
{
  "ran_at": "2026-05-25T08:00:00Z",
  "window": 100,
  "dimensions": {
    "input_tokens": {
      "mean": 412.3,
      "stdev": 89.1,
      "anomalies_z": [],
      "anomalies_iqr": []
    },
    "output_tokens": {
      "mean": 285.4,
      "stdev": 142.0,
      "anomalies_z": [
        {"seq": 5847, "value": 4096, "z": 26.83}
      ],
      "anomalies_iqr": [
        {"seq": 5847, "value": 4096}
      ]
    }
  },
  "total_anomalies": 1
}
```

In this example, seq 5847 hit `max_tokens` (4096) — likely a runaway
loop or a prompt that elicited the full token budget. The signal lets
the operator investigate that specific entry via the dashboard.

## Real production catch

On 2026-05-23 GENA's regression cron flagged a span where Sonnet
returned 3,847 output tokens against a 90-token historical mean — a
38× spike. Inspecting via `bijotel inspect --seq 5234` showed the
prompt had asked for "a complete report" without `max_tokens`,
resulting in a $0.08 burn on a single call. The signal preceded the
billing alert by 4 hours.

## Next

- [Dashboard / Regression Monitor](dashboard.md#regression-monitor-regression)
- [REST API: /regression/*](../api/rest.md#regression)
