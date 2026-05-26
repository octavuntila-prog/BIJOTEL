"""``bijotel.replay`` — deterministic-seed replay verification (v2.7.0).

BIJOTEL has always answered "was this entry tampered with?" via the HMAC
chain. This module extends the question to "if I re-run the same call with
the same model + seed + temperature, do I get the same output?"

Replay verification cannot read the original prompt from the chain — the
chain stores hashes, not content (by design: keeps PII out, keeps rows
small). The caller is expected to bring the prompt from their own
application-side store (Langfuse, log archive, ...). What BIJOTEL proves
is that *the output hash matches what was sealed at the time the call
was logged*.

Public API:
    ``record_replay_context(...)`` — build span attrs for an LLM call.
    ``verify_replay(chain_entry, replayed_output)`` — compare hashes.
    ``ReplayResult``               — dataclass with the comparison verdict.
"""

from __future__ import annotations

from bijotel.replay.recorder import record_replay_context
from bijotel.replay.verifier import ReplayResult, verify_replay

__all__ = [
    "ReplayResult",
    "record_replay_context",
    "verify_replay",
]
