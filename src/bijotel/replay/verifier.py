"""Verifier — compare a chain entry's sealed output_hash with a replay.

Two usage paths:

1. **Library**: pass the parsed canonical body of a chain entry as a
   dict and the replayed output string; get back a ``ReplayResult``.
2. **From DB**: load the entry via ``bijotel inspect`` / SQL, parse its
   ``canonical_body`` (handled by the CLI command in
   ``cli/cmd_replay.py``), then call this verifier.

The verifier never calls an LLM. It is pure hash comparison.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of comparing a sealed ``output_hash`` against a replay.

    Attributes:
        match: ``True`` iff the replay hash equals the sealed hash.
        original_hash: The ``bijotel.replay.output_hash`` from the chain
            entry, or ``None`` if the entry was logged before v2.7.0
            (no replay metadata present).
        replay_hash: SHA-256 hex of the provided replayed output. Always
            populated, even when ``match`` is ``False`` — the caller
            uses it to feed back into further analysis.
        deterministic: The sealed ``bijotel.replay.deterministic`` flag.
            ``False`` means the original call did not set a seed, so a
            mismatch is *expected* (still a useful signal — it tells you
            "this can't be replayed deterministically").
        model_version: The sealed ``bijotel.replay.model_version`` —
            useful to surface in the result so the caller can spot
            "different model now than at log time" before assuming the
            mismatch means tampering.
        reason: Human-readable explanation when ``match`` is ``False``,
            or ``None`` when the result was a clean match. Helps the
            CLI/REST surface produce specific error messages without
            re-deriving them.
    """

    match: bool
    original_hash: str | None
    replay_hash: str
    deterministic: bool
    model_version: str | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form for JSON serialization (REST endpoint)."""
        return asdict(self)


def verify_replay(
    chain_entry: dict[str, Any],
    replayed_output: str,
) -> ReplayResult:
    """Compare a chain entry's sealed output hash against a replayed string.

    Args:
        chain_entry: A dict with shape ``{"attributes": {...}}``. The
            attribute namespace is ``bijotel.replay.*`` (see
            :mod:`bijotel.replay.recorder`). Other span fields (name,
            kind, status) are ignored — only the replay attributes
            matter here. This is the shape produced by
            ``json.loads(canonical_body)`` from a chain.db row.
        replayed_output: The text returned by re-executing the same
            prompt against the same model. Must be the post-formatting
            string (don't pass the raw provider response object).

    Returns:
        ``ReplayResult`` with the verdict. Always returns; never raises
        on missing metadata — instead, the result carries
        ``match=False`` and a ``reason`` describing what was missing.

    Examples:

        >>> entry = {"attributes": {
        ...     "bijotel.replay.output_hash": "abc...",
        ...     "bijotel.replay.deterministic": True,
        ...     "bijotel.replay.model_version": "claude-haiku-4-5",
        ... }}
        >>> result = verify_replay(entry, "the replayed answer")
        >>> result.match
        False
    """
    attrs = chain_entry.get("attributes", {}) or {}
    original_hash = attrs.get("bijotel.replay.output_hash")
    deterministic = bool(attrs.get("bijotel.replay.deterministic", False))
    model_version = attrs.get("bijotel.replay.model_version")

    replay_hash = hashlib.sha256(replayed_output.encode("utf-8")).hexdigest()

    if original_hash is None:
        return ReplayResult(
            match=False,
            original_hash=None,
            replay_hash=replay_hash,
            deterministic=deterministic,
            model_version=model_version,
            reason=(
                "chain entry has no bijotel.replay.output_hash "
                "(logged before v2.7.0 or replay metadata not attached)"
            ),
        )

    if original_hash == replay_hash:
        return ReplayResult(
            match=True,
            original_hash=original_hash,
            replay_hash=replay_hash,
            deterministic=deterministic,
            model_version=model_version,
            reason=None,
        )

    # Mismatch — give the caller useful context.
    if not deterministic:
        reason = (
            "output hash mismatch — original call did not record a seed "
            "(deterministic=False); replay drift is expected"
        )
    else:
        reason = (
            "output hash mismatch — re-execute against the SAME model "
            f"version that produced the original ({model_version!r}) "
            "and verify the prompt hash before treating this as tampering"
        )

    return ReplayResult(
        match=False,
        original_hash=original_hash,
        replay_hash=replay_hash,
        deterministic=deterministic,
        model_version=model_version,
        reason=reason,
    )
