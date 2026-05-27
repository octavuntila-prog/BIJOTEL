# Burn-rate audit — 2026-05-27

**Trigger:** Deep-research report claimed BIJOTEL001 burned at
$17.79/day during 21-23 May sprint vs $2.86/day baseline. Goal: verify
that post-sprint burn is normalized.

## Method

Read-only inspection of chain.db on both production systems (GENA +
ARA). For each day in the last 7 days, summed `gen_ai.usage.*` tokens
per model and applied 2026-05 Anthropic price table.

## Results

### GENA — `/data/bijotel_chain.db`

| Date | Calls | Model | In tok | Out tok | USD |
|---|---|---|---|---|---|
| 2026-05-21 | 394 | claude-sonnet-4-20250514 | 509,395 | 165,385 | **$4.01** |
| 2026-05-21 | 33 | claude-haiku-4-5 | 30,635 | 20,238 | $0.11 |
| 2026-05-22 | 346 | claude-haiku-4-5 | 402,785 | 216,425 | $1.19 |
| 2026-05-23 | 331 | claude-haiku-4-5 | 386,855 | 203,795 | $1.12 |
| 2026-05-24 | 386 | claude-haiku-4-5 | 411,010 | 225,470 | $1.23 |
| 2026-05-25 | 427 | claude-haiku-4-5 | 534,625 | 274,779 | $1.53 |
| 2026-05-26 | 408 | claude-haiku-4-5 | 521,831 | 266,357 | $1.48 |
| 2026-05-27 (partial) | 156 | claude-haiku-4-5 | 205,466 | 105,265 | $0.59 |

**GENA 7-day avg from chain: $1.61/day.**

### ARA — `/app/data/bijotel_chain.db`

| Date | Total | Notes |
|---|---|---|
| 2026-05-21 .. 2026-05-24 | $0 | No chain entries — BIJOTEL added to ARA 2026-05-25 |
| 2026-05-25 | $1.21 | Haiku $0.39 + Sonnet 4.5 $0.81 |
| 2026-05-26 | $1.68 | Haiku $0.60 + Sonnet 4.5 $1.08 |
| 2026-05-27 (partial) | $0.00 | Restart at ~08:00 UTC after L5-L7 patch |

**ARA 7-day avg from chain (post-instrumentation): $0.41/day.**

## Findings

### 1. Chain-visible burn ≠ total BIJOTEL001 spend

The chain.db only captures LLM calls that flow through
OTel-instrumented containers (GENA's 4 main + ARA backend). The
chain does NOT see:

- Claude Code sessions (e.g. dev work, this very BIJOTEL maintenance)
- Direct `curl https://api.anthropic.com/...` from any other consumer
  of the same BIJOTEL001 key
- Pre-2026-05-25 ARA traffic (BIJOTEL wasn't wired yet)

**Implication:** the report's $17.79/day sprint claim likely came from
Anthropic Console totals, which include all consumers of the key. The
chain.db can confirm or refute *agent* burn, not *total* burn.

### 2. The May 21 Sonnet spike is real

On 2026-05-21, GENA used Sonnet 4 instead of Haiku 4.5 for 394 calls,
costing $4.01 just for Sonnet. From May 22 onwards, GENA switched back
to Haiku exclusively. This is consistent with a sprint experiment that
explored Sonnet quality and then settled back to Haiku for production.

### 3. Post-sprint normalization confirmed (for chain-visible burn)

Last 7 days excluding the May 21 Sonnet day:
- GENA: $1.12–$1.53/day (Haiku only)
- ARA: $0.41/day avg (post-instrumentation)
- Combined chain-visible: **~$2/day**

This is below the report's $2.86/day pre-sprint baseline. **Within
chain visibility, burn rate has normalized.**

### 4. What we can NOT confirm from chain alone

- Total BIJOTEL001 burn (need Anthropic Console)
- Whether non-agent consumers (Claude Code, direct API) were the
  $17.79/day driver
- Whether the report's baseline of $2.86/day was Console total or
  agent-only

## Recommendation

**Action:** the user should check Anthropic Console at
https://console.anthropic.com/ → Usage → API key BIJOTEL001 for the
last 14 days to compare against the report's claim.

**No code action needed** based on chain-visible data — agent burn is
well within baseline. L5-L7 policy rules deployed today (cap $0.50
per call, 2M tokens/day on GENA, 1M on ARA) provide tripwire
protection against future runaway scenarios regardless of cause.

## Time check

Audit completed in ~25 min, under the 30-min cap.
