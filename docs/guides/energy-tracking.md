# Energy & Carbon Tracking

Every LLM call has an energy cost. BIJOTEL estimates Wh and gCO₂ per
call based on model and token count, then aggregates per region's grid
intensity.

## Why

- **Carbon reporting** for ESG / sustainability claims
- **Cost-of-energy** as a routing signal (some models are 8× more
  efficient per token)
- **Provider migration justification** — show the real CO₂ delta
  before committing

## Backfill existing chain

If you already have a `chain.db` from before energy tracking was
wired, run a one-shot backfill:

```bash
bijotel energy backfill --db chain.db --region us-east
```

This populates an `energy_log` table linked to `chain.seq`. Idempotent
— re-running doesn't double-count.

## REST API

```bash
curl http://localhost:8080/api/energy/summary
```

```json
{
  "total_wh": 19.95,
  "total_co2_grams": 7.58,
  "total_calls": 5459,
  "by_model": {
    "claude-haiku-4-5-20251001": {"wh": 8.21, "co2_grams": 3.12, "calls": 4102},
    "claude-sonnet-4-20250514":  {"wh": 11.74, "co2_grams": 4.46, "calls": 1357}
  },
  "by_region": {
    "us-east": {"wh": 19.95, "co2_grams": 7.58, "calls": 5459}
  }
}
```

Estimate energy for a hypothetical call:

```bash
curl -X POST http://localhost:8080/api/energy/estimate \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","tokens":1500,"region":"us-east"}'
# → {"wh": 0.0015, "co2_grams": 0.00057}
```

## Real production example (GENA)

15 days of operation, 5,459 LLM-call rows in `energy_log`:

- **Total**: 19.95 Wh / 7.58 g CO₂
- **Per call avg**: 3.66 mWh / 1.39 mg CO₂

The migration from Sonnet to Haiku on 2026-05-21 cut daily CO₂ **≈ 8×**
— captured retroactively, not designed in. The signal lives in the
chain forever and is auditable.

## Energy model

The estimate is intentionally simple — Wh per 1K tokens, looked up per
model, then converted to gCO₂ via region intensity:

```python
wh = (tokens / 1000) * rate_wh_per_1k_tokens[model]
co2_grams = (wh / 1000) * grid_intensity_g_per_kwh[region]
```

The rate dictionary `DEFAULT_ENERGY_RATES` and the regional grid
intensity `DEFAULT_GRID_INTENSITY` are public constants in
`bijotel.layers.energy`:

```python
from bijotel.layers.energy import DEFAULT_ENERGY_RATES, DEFAULT_GRID_INTENSITY

DEFAULT_ENERGY_RATES["claude-haiku-4-5-20251001"]  # 0.001  (Wh per 1K tokens)
DEFAULT_GRID_INTENSITY["us-east"]                  # 380.0  (gCO₂ per kWh)
```

Override with your own dicts for higher fidelity.

## Supported regions

| Region | gCO₂/kWh | Notes |
|---|---|---|
| `us-east` | 380 | Virginia (Anthropic, AWS us-east-1) |
| `us-west` | 250 | Oregon |
| `eu-west` | 300 | Ireland |
| `eu-central` | 350 | Germany / Frankfurt |
| `eu-north` | 30 | Sweden / Norway (hydropower) |

Custom regions: pass `intensity_overrides={"my-region": 120.0}` to
`CarbonCalculator`.

## Accuracy

R3-E4 production test: **20/20 = 100% exact match** between
`energy_log` rows and manual recompute from
`DEFAULT_ENERGY_RATES` × `DEFAULT_GRID_INTENSITY`.

The rates themselves are estimates (we don't measure your data
center). What this validates is that **the engine's storage matches
its config** — i.e. no silent rate drift between live calculation
and persisted records.

## Next

- [Dashboard / Energy panel](dashboard.md)
- [REST API: /energy/*](../api/rest.md#energy)
