"""Recorder — build ``bijotel.replay.*`` attributes for an LLM call.

Call ``record_replay_context(...)`` *after* the LLM returned, and feed
its result into ``span.set_attribute`` in a loop. The attributes flow
into the canonical body via the existing capture-everything path in
``span_to_canonical_dict`` (no special handling needed).

The two hashes — ``prompt_hash`` and ``output_hash`` — are deliberately
truncated to SHA-256 hex. They are *fingerprints*, not the content. The
chain never sees the raw prompt or response.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _hash_prompt(prompt: str | list[dict[str, Any]]) -> str:
    """SHA-256 hex of the prompt.

    Strings hash their UTF-8 bytes directly. List-of-dicts (the OTel
    GenAI messages shape) are JSON-encoded with sorted keys for stable
    hashing — same prompt, same hash, even across different SDKs.
    """
    if isinstance(prompt, str):
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    # JSON normalization: sorted keys, compact separators. Identical
    # content → identical bytes → identical hash.
    encoded = json.dumps(
        prompt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_replay_context(
    *,
    prompt: str | list[dict[str, Any]],
    output: str,
    model: str,
    seed: int | None = None,
    temperature: float = 1.0,
    top_p: float = 1.0,
    model_version: str | None = None,
) -> dict[str, Any]:
    """Build the ``bijotel.replay.*`` attribute dict for an LLM call.

    The returned dict is suitable for ``span.set_attribute`` in a loop
    (all values are OTel-attribute-primitive). Keys live under the
    ``bijotel.replay.*`` namespace and are sealed by the canonical body
    automatically.

    Args:
        prompt: The full prompt sent to the LLM. Either the joined
            string form or the GenAI messages list (``[{"role":"user",
            "content":"..."}]``).
        output: The model's response text. For multi-message responses,
            join the parts to a single string before passing in — the
            hash is sensitive to formatting.
        model: Model identifier as exposed via ``gen_ai.request.model``
            (e.g. ``claude-haiku-4-5-20251001``).
        seed: Optional integer random seed. ``None`` means
            "non-deterministic" — recorded honestly so the verifier
            knows replay is best-effort.
        temperature: Sampling temperature. Default 1.0 matches typical
            SDK defaults; ``0.0`` is the deterministic-replay choice.
        top_p: Nucleus-sampling cutoff. Default 1.0.
        model_version: Optional explicit model checkpoint string. Falls
            back to ``model`` when omitted — many providers don't
            surface a separate checkpoint id.

    Returns:
        Dict with keys ``bijotel.replay.prompt_hash``,
        ``.output_hash``, ``.seed`` (only when not ``None``),
        ``.temperature``, ``.top_p``, ``.model_version``,
        ``.deterministic`` (bool: ``True`` iff ``seed`` was set).

    Note:
        ``seed`` is omitted from the result when ``None``, rather than
        recorded as null. OTel attributes don't have a clean null
        primitive; omission is the cleaner representation of "not set".
    """
    attrs: dict[str, Any] = {
        "bijotel.replay.prompt_hash": _hash_prompt(prompt),
        "bijotel.replay.output_hash": hashlib.sha256(
            output.encode("utf-8")
        ).hexdigest(),
        "bijotel.replay.temperature": float(temperature),
        "bijotel.replay.top_p": float(top_p),
        "bijotel.replay.model_version": model_version or model,
        "bijotel.replay.deterministic": seed is not None,
    }
    if seed is not None:
        attrs["bijotel.replay.seed"] = int(seed)
    return attrs
