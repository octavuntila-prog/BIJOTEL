# Dashboard

`bijotel serve --dashboard` launches a FastAPI app that serves both
the REST API and a React-based dashboard.

## Start

```bash
bijotel serve --port 8080 --host 0.0.0.0 --db chain.db --dashboard
# → http://localhost:8080
```

Production deployments typically run this behind a reverse proxy
(nginx, Caddy, Cloudflare Tunnel) for TLS.

## Pages

### Chain Explorer (`/`)

Browse every chain entry. Search, filter, paginate. Click an entry
for the detail panel with:

- Span name and trace/span IDs
- Canonical body (JCS-canonicalized JSON)
- `hmac_hash` and `prev_hash`
- A **Verify** button that re-computes the HMAC inline

### Policy Dashboard (`/policy`)

Active rules grid + a live evaluate form: type any prompt, hit
Evaluate, see which rules fire in real time.

Useful for:

- Testing custom rules against a corpus
- Debugging false positives in `prompt_pattern_deny`
- Showing compliance auditors how the policy gate works

### Regression Monitor (`/regression`)

- Anomaly timeline chart (recharts)
- Dimension breakdown table (input/output tokens, cost, latency)
- "Run regression now" button
- History of past runs

### System Status (`/system`)

Grid of all **14 bijuterii layers**:

| Layer | What it does | Active when |
|---|---|---|
| F1 Schema Discovery | OTel GenAI conformance | always (semantic conventions used) |
| F2 HMAC Chain | Tamper-evident audit | always (default ON) |
| F3 CAS | Content-addressable storage | always (default ON) |
| F4 PolicyEngine | Pre-call gating | engine has rules |
| F5 Decorator | `@trace_genai`, `@wrap` | imported |
| F6 Energy | Wh + CO₂ accounting | `[fingerprint]` extra installed |
| F7 Provider Protocol | Adapter pattern | always |
| F9 OpenAIAdapter | Validates F7 | imported |
| F11 Prompt Pattern Deny | 50 jailbreak patterns | engine has the rule |
| F12 Regression Detection | z-score + IQR drift | `bijotel regression` ran |
| F14 AST Safety | tree-sitter bash + Python ast | `[ast]` extra installed |
| F15 Routing Recommender | Pareto cost/quality | rule wired |
| F18 Misalignment Probes | 29 probes / 8 categories | probe JSON exists |
| F19 OTel GenAI Semconv | Compatible with all instrumentors | always |

## Authentication

Set `BIJOTEL_API_KEY` to enforce Bearer auth on all `/api/*`
endpoints:

```bash
BIJOTEL_API_KEY=$(python -c "import secrets; print(secrets.token_hex(32))") \
  bijotel serve --dashboard --db chain.db
```

Then:

```bash
curl -H "Authorization: Bearer YOUR_KEY" http://localhost:8080/api/chain
```

**Public paths** (bypass auth, for convenience and dashboard rendering):

- `/api/health`, `/api/version`
- `/docs`, `/redoc`, `/openapi.json`
- `/` (SPA root), `/assets/*` (SPA bundle)

R3-A2 production test validated 10/10 auth scenarios (5 invalid → 401,
2 valid → 200, 2 public bypass → 200, lowercase `bearer` accepted per
RFC 7235).

## Custom policy engine

Pass your own `PolicyEngine` to control which rules the dashboard
exposes:

```python
from bijotel.api import create_app
from bijotel import PolicyEngine, prompt_pattern_deny

engine = PolicyEngine(rules=[
    prompt_pattern_deny(mode="deny"),  # block, don't just warn
    # ...your rules
])

app = create_app(policy_engine=engine, db_path="chain.db")
# uvicorn ... app
```

## Performance

R3-D1 measured on the GENA deployment:

| Endpoint | p50 | p95 | p99 |
|---|---|---|---|
| `/api/health` | 1.2 ms | 9.9 ms | 9.9 ms |
| `/api/chain?limit=10` | 1.5 ms | 2.1 ms | 2.1 ms |
| `/api/chain/stats` | 15.6 ms | 23.9 ms | 23.9 ms |
| `/api/policy/evaluate` | 1.9 ms | 4.1 ms | 4.1 ms |
| `/api/chain/verify` (smoke) | 2.9 ms | 4.2 ms | 4.2 ms |

## Next

- [REST API Reference](../api/rest.md) for every endpoint
- [Policy Engine](policy-engine.md) for custom rules
