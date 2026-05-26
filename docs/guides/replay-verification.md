# Replay Verification

**Available from v2.7.0.**

Until v2.6.0, BIJOTEL answered one question:
**"Has this chain entry been tampered with?"** v2.7.0 adds a second one:
**"If I rerun the same call with the same model and seed, do I get the
same output?"**

If the answer is no, *either* the model changed, *or* the seed/parameter
recording is wrong, *or* the chain was tampered with after sealing. Replay
verification turns "tamper-evident" into "tamper-evident plus
reproducibility-evident" — without bringing in zkLLM machinery.

## What gets recorded

When you call `record_replay_context(...)`, BIJOTEL writes these attributes
into the span before the LLM call returns:

| Attribute                            | Type   | Purpose                                          |
|--------------------------------------|--------|--------------------------------------------------|
| `bijotel.replay.prompt_hash`         | str    | SHA-256 of the full prompt                       |
| `bijotel.replay.output_hash`         | str    | SHA-256 of the LLM response                      |
| `bijotel.replay.seed`                | int    | Random seed used (only present when set)         |
| `bijotel.replay.temperature`         | float  | Sampling temperature                             |
| `bijotel.replay.top_p`               | float  | Nucleus-sampling cutoff                          |
| `bijotel.replay.model_version`       | str    | Exact model identifier / checkpoint              |
| `bijotel.replay.deterministic`       | bool   | True iff a seed was passed                       |

Note: **prompt and output bytes never enter the chain.** Only their hashes
do. That keeps PII out of the audit trail and keeps row sizes small. The
trade-off is that replay requires the original prompt from a separate
store (Langfuse, application logs, ...). See *Honest limitations* below.

## Recording at call time

```python
from bijotel.replay import record_replay_context

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    messages=[{"role": "user", "content": prompt}],
    temperature=0.0,
    extra_body={"seed": 42},  # SDK-specific seed field
)
output_text = response.content[0].text

# Inside your active span:
attrs = record_replay_context(
    prompt=prompt,
    output=output_text,
    model="claude-haiku-4-5-20251001",
    seed=42,
    temperature=0.0,
)
for k, v in attrs.items():
    span.set_attribute(k, v)
```

Calls without a seed are still recordable — pass `seed=None` (or just omit
it). The attribute set then has `deterministic=False`, and downstream
verifiers know replay drift is *expected*. That honesty matters more
than pretending every call is reproducible.

## Verifying a replay

You re-execute the call (same model, same seed, same temperature) in some
other context — your replay harness, a forensic notebook, an auditor's
sandbox. You hand the resulting text to BIJOTEL; BIJOTEL compares hashes.

### CLI

```
$ bijotel replay --db chain.db --seq 6373 --output "the replayed answer"
=== Replay MATCH (seq=6373) ===
  original_hash:  3f2a...
  replay_hash:    3f2a...
  deterministic:  True
  model_version:  claude-haiku-4-5-20251001
```

Or from a file (for multi-line outputs):

```
$ bijotel replay --db chain.db --seq 6373 --output-file replay.txt
```

Exit codes:

- `0` — match
- `1` — DB/file/seq missing
- `2` — argument errors (e.g. neither `--output` nor `--output-file`)
- `3` — mismatch

### REST

```
$ curl -X POST http://localhost:8080/replay/verify \
       -H 'Content-Type: application/json' \
       -d '{"seq": 6373, "replayed_output": "the replayed answer"}'

{
  "match": true,
  "original_hash": "3f2a...",
  "replay_hash": "3f2a...",
  "deterministic": true,
  "model_version": "claude-haiku-4-5-20251001",
  "reason": null
}
```

A mismatch still returns HTTP 200 — the comparison itself succeeded; the
*answer* is "no match". The body carries `match: false` plus a `reason`
field that explains what was different:

- **deterministic=true mismatch**: `output hash mismatch — re-execute
  against the SAME model version that produced the original (...)`.
- **deterministic=false mismatch**: `output hash mismatch — original call
  did not record a seed (deterministic=False); replay drift is expected`.
- **no original hash present**: `chain entry has no
  bijotel.replay.output_hash (logged before v2.7.0 or replay metadata
  not attached)`.

HTTP 4xx is reserved for "the comparison itself couldn't run" (e.g. 404
when the requested seq isn't in the chain).

### Python API

```python
from bijotel.replay import verify_replay
import json
import sqlite3

with sqlite3.connect("chain.db") as conn:
    body = conn.execute(
        "SELECT canonical_body FROM chain WHERE seq = 6373"
    ).fetchone()[0]
entry = json.loads(body)

result = verify_replay(entry, replayed_output="the replayed answer")
print(result.match, result.reason)
```

`verify_replay` always returns a `ReplayResult`; it never raises. Use the
dataclass's fields directly, or `result.to_dict()` for serialization.

## Honest limitations

1. **BIJOTEL stores hashes, not content.** The chain never sees the
   prompt or output. To replay, you need the original prompt from a
   separate store. BIJOTEL proves the *output hash* matches what it
   sealed; the rest of the loop (re-execute against the model) is on
   the caller's pipeline.
2. **Model versions matter.** When a provider updates a model
   silently, a clean prompt + seed + temperature replay can drift. The
   `bijotel.replay.model_version` field is recorded so an investigator
   can see this — the model that runs today may not be the model that
   produced the sealed hash.
3. **Seed support is provider-specific.** Anthropic and OpenAI both
   document a seed field; in practice, near-identical outputs are
   common but not guaranteed. Treat replay match as a strong signal,
   replay mismatch as a *useful* signal that wants triage, not as
   proof of tampering.
4. **No live LLM call from BIJOTEL.** `bijotel replay` never calls
   any model. It is pure SHA-256 comparison. If you want a re-execution
   harness, build it next to BIJOTEL, not inside it.

## When to use

- **CI on prompt changes.** Stash a known-good chain entry, edit your
  prompt template, replay against the same seed/model — drift surfaces
  the moment behavior changes.
- **Incident response.** A model output is disputed; replay against the
  sealed entry to show the system did or did not produce that text.
- **Audit support.** Regulators asking "are you sure this is the output
  you logged?" can rerun and check the hash themselves, on their own
  hardware.

## Public API

```python
from bijotel.replay import (
    record_replay_context,   # build span attrs
    verify_replay,           # compare against a replay
    ReplayResult,            # verdict dataclass
)
```

Each is re-exported from `bijotel` directly:

```python
from bijotel import record_replay_context, verify_replay, ReplayResult
```
