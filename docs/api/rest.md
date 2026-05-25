# REST API Reference

Started via `bijotel serve [--dashboard]`. FastAPI under the hood — every
endpoint also visible at `/docs` (Swagger UI) and `/redoc`.

Base URL: `http://localhost:8080/api`

## Endpoints

| Method | Path | Description | Auth |
|---|---|---|---|
| GET | `/health` | Liveness + version + db_exists | No |
| GET | `/version` | Package version | No |
| GET | `/chain` | List chain entries (paginated) | Yes |
| GET | `/chain/stats` | Chain statistics | Yes |
| GET | `/chain/{seq}` | One entry's full canonical body | Yes |
| POST | `/chain/verify` | Verify chain integrity (smoke or full) | Yes |
| GET | `/policy/rules` | Active policy rules + introspection | Yes |
| POST | `/policy/evaluate` | Evaluate a prompt against the engine | Yes |
| GET | `/layers` | Status of every BIJOTEL bijuterie | Yes |
| GET | `/regression/latest` | Latest regression result | Yes |
| GET | `/regression/history` | Regression run history | Yes |
| POST | `/regression/run` | Run regression on demand | Yes |
| POST | `/export` | Download a signed chain export | Yes |
| POST | `/export/verify` | Verify an uploaded export JSON | Yes |
| POST | `/containment/evaluate` | F4 + F14 combo decision | Yes |
| POST | `/consensus/evaluate` | Multi-model consensus voting | Yes |
| GET | `/consensus/stakes` | Stakes classification | Yes |
| GET | `/energy/summary` | Aggregate Wh + gCO₂ | Yes |
| POST | `/energy/estimate` | Estimate energy for a hypothetical call | Yes |

Plus framework routes outside `/api`:

- `/docs` — Swagger UI (Yes, no auth)
- `/redoc` — Redoc UI (no auth)
- `/openapi.json` — OpenAPI spec (no auth)
- `/` and `/assets/*` — Dashboard SPA bundle (no auth)

## Authentication

If `BIJOTEL_API_KEY` env var is set, all `/api/*` endpoints require
`Authorization: Bearer <key>`.

```bash
curl -H "Authorization: Bearer YOUR_KEY" http://localhost:8080/api/chain
```

Public paths bypass auth (see [Dashboard guide](../guides/dashboard.md#authentication)).

## /chain

### `GET /api/chain?limit=50&offset=0`

```json
{
  "entries": [
    {
      "seq": 5889,
      "timestamp_ns": 1716628800000000000,
      "trace_id": "abc...",
      "span_id": "def...",
      "span_name": "anthropic.chat",
      "canonical_hash": "02db0af4...",
      "hmac_hash": "1370b3a2..."
    }
  ],
  "total": 5889
}
```

### `GET /api/chain/{seq}`

Full entry including base64-encoded canonical body.

### `POST /api/chain/verify`

```bash
# Smoke (fast — verifies first + last only)
curl -X POST http://localhost:8080/api/chain/verify \
  -H "Content-Type: application/json" \
  -d '{"full": false}'

# Full (slow — every entry)
curl -X POST http://localhost:8080/api/chain/verify \
  -H "Content-Type: application/json" \
  -d '{"full": true}'
```

```json
{"valid": true, "entries_verified": 2, "first_seq": 1, "last_seq": 5889}
```

## /policy

### `GET /api/policy/rules`

Lists every rule the engine has, with `mode` and config introspection.

### `POST /api/policy/evaluate`

```bash
curl -X POST http://localhost:8080/api/policy/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role":"user","content":"Hello"}],
    "model": "claude-haiku-4-5-20251001"
  }'
```

```json
{
  "decision": "allow",
  "denied": false,
  "deny_rule": null,
  "deny_reason": null,
  "warnings": [],
  "evaluation_ms": 1.234
}
```

## /regression

### `GET /api/regression/latest`

Latest run's anomalies, cached for fast UI rendering.

### `POST /api/regression/run`

```bash
curl -X POST http://localhost:8080/api/regression/run \
  -H "Content-Type: application/json" \
  -d '{"window": 100, "z_threshold": 3.0}'
```

## /export

### `POST /api/export`

Streams a signed JSON of the entire chain as a downloadable file.

```bash
curl -X POST http://localhost:8080/api/export \
  -o my_chain_export.json
```

### `POST /api/export/verify`

Multipart upload of an export file → server verifies → JSON response.

```bash
curl -X POST http://localhost:8080/api/export/verify \
  -F "file=@my_chain_export.json" \
  -H "X-Secret-Hex: deadbeef..."
```

## /energy

### `GET /api/energy/summary`

See [Energy Tracking guide](../guides/energy-tracking.md#rest-api).

### `POST /api/energy/estimate`

```bash
curl -X POST http://localhost:8080/api/energy/estimate \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","tokens":1500,"region":"us-east"}'
```

## /layers

```bash
curl http://localhost:8080/api/layers
```

Returns the status of every BIJOTEL bijuterie (`active` / `available`
/ `planned`), used by the System Status dashboard page.

## OpenAPI

Full machine-readable spec at `/openapi.json`. Import into Postman /
Insomnia / your client of choice.

## Next

- [Dashboard guide](../guides/dashboard.md) — what the UI does with these
- [CLI](cli.md) — same operations from the shell
- [Python API](python.md) — programmatic
