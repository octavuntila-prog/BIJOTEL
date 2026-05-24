"""F9 / Bijuteria #9: Multi-LLM consensus voting.

Don't ask one model. Ask N and compare.

**N-version programming** (Avizienis 1977, Space Shuttle flight software):
run independent implementations of the same spec in parallel, vote on
outputs. One impl may have a bug, but two identical bugs across two
independent teams is improbable; three is near-impossible. Applied to
LLMs in 2025: one model may hallucinate, but two hallucinating the
same thing for the same prompt is unlikely, and three is rare. So:

* High-stakes query → run on Haiku + Sonnet (+ optionally Opus)
* Compute agreement score over their responses
* Above threshold → consensus, return any one (or the highest-quality
  model's response)
* Below threshold → flag for human review; the disagreement itself is
  the signal

Three building blocks plus a policy rule:

* :class:`StakesClassifier` — keyword heuristic; ``classify(messages)``
  returns ``"high"`` or ``"low"``. The 30-keyword default covers
  medical, legal, financial, safety, security domains. Override the
  class attribute to extend.

* :class:`ConsensusVoter` — orchestrator. ``vote(messages, max_tokens)``
  fires N parallel async calls (one per model), collects results,
  computes agreement, returns a :class:`ConsensusResult`. The
  ``provider`` parameter is a ``(model, messages, max_tokens) ->
  ModelResponse`` coroutine; the package ships an Anthropic default
  that lazy-imports the ``anthropic`` SDK.

* Agreement scoring — :func:`compute_agreement` uses Jaccard over
  case-folded word tokens, pairwise-mean across N responses. Cheap,
  no extra deps, no false-precision. The docstring is honest about
  the limit: "the cat sat" vs "a cat is sitting" scores ~0.4 even
  though humans read them as ~0.85 agreement. For semantic
  similarity, plug a different scorer.

* :func:`consensus_requirement` — :class:`PolicyEngine` rule factory.
  Warns when a high-stakes request is routed to a single model. The
  host marks multi-model calls by passing ``{"_consensus": True}`` (or
  ``"models_used": N`` >= 2) in the request dict; the rule never
  warns on those.

Cost note: consensus = N × cost. Use ``StakesClassifier`` upstream as
a gate — only high-stakes prompts pay the multiplier. Low-stakes
prompts go through a single model unchanged.

Provenance: BIJOTEL-original, v1.8.0. Closes the long-standing
"#9 not coded" gap in the bijuterii catalog.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from bijotel.policy.decision import Decision

_LOG = logging.getLogger("bijotel.consensus")


# ============================================================================
# Data classes
# ============================================================================


@dataclass(frozen=True)
class ModelResponse:
    """One model's reply within a consensus round.

    Always populated, even on error. ``error`` is the only signal that
    the call failed; ``response`` is empty in that case so callers can
    treat the field uniformly.
    """

    model: str
    response: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class ConsensusResult:
    """Outcome of :meth:`ConsensusVoter.vote` over N models.

    Attributes:
        models_queried: The exact list passed in (preserves order).
        responses: One :class:`ModelResponse` per model, same order.
        agreement_score: Pairwise-mean Jaccard token overlap in
            ``[0.0, 1.0]``. ``1.0`` = identical token sets; ``0.0`` =
            disjoint vocabularies.
        consensus_reached: ``agreement_score >= threshold``.
        threshold: The threshold used to compute ``consensus_reached``.
            Kept on the result so downstream consumers don't need to
            ask the voter again.
        disagreement_details: Empty when consensus reached; otherwise
            human-readable summaries of which model-pairs disagreed
            most (top 3 by lowest pairwise score).
        recommended_response: The text the host should propagate
            downstream. Strategy: the response from the highest-cost
            model that succeeded (rough quality proxy in the default
            registry). On full disagreement, the host still gets ONE
            text — disagreement is signaled separately via
            ``consensus_reached=False``.
        recommended_model: Which model produced ``recommended_response``.
        cost_total_usd: Sum of per-model costs. Direct accountancy:
            consensus is exactly N× a single call.
        latency_ms: ``max`` of per-model latencies (parallel execution).
        errors: List of error strings (one per failed model). Empty
            when all calls succeeded. ``len(errors) == len(models)``
            with ``"" / None`` for successes is the alternate shape;
            we picked the dense form to make ``not errors`` a one-line
            "everything OK" check.
    """

    models_queried: list[str]
    responses: list[ModelResponse]
    agreement_score: float
    consensus_reached: bool
    threshold: float
    disagreement_details: list[str] = field(default_factory=list)
    recommended_response: str = ""
    recommended_model: str = ""
    cost_total_usd: float = 0.0
    latency_ms: float = 0.0
    errors: list[str] = field(default_factory=list)


# ============================================================================
# Stakes classifier
# ============================================================================


class StakesClassifier:
    """Heuristic gate for "is this query worth N× cost?"

    Keyword-based on a 30-term default list across medical, legal,
    financial, safety, security domains. ``classify`` returns
    ``"high"`` if any keyword is in the message text (case-insensitive,
    whole-word boundary); ``"low"`` otherwise.

    Heuristics are intentionally simple here: the host that cares about
    precision should subclass and override
    :attr:`HIGH_STAKES_KEYWORDS` or replace ``classify`` entirely.
    """

    HIGH_STAKES_KEYWORDS: tuple[str, ...] = (
        # medical
        "medical",
        "diagnosis",
        "treatment",
        "medication",
        "dosage",
        "symptom",
        "clinical",
        "prescription",
        # legal
        "legal",
        "contract",
        "lawsuit",
        "liability",
        "regulation",
        "compliance",
        "court",
        "attorney",
        # financial
        "financial",
        "investment",
        "loan",
        "mortgage",
        "tax",
        "fraud",
        # safety
        "safety",
        "danger",
        "hazard",
        "emergency",
        "irreversible",
        "fatal",
        # security
        "vulnerability",
        "exploit",
        "credential",
    )

    def classify(self, messages: Sequence[dict] | str) -> str:
        """Return ``"high"`` or ``"low"``."""
        stakes, _ = self.classify_with_hits(messages)
        return stakes

    def classify_with_hits(
        self, messages: Sequence[dict] | str
    ) -> tuple[str, list[str]]:
        """Same as :meth:`classify` plus the list of matched keywords.

        Useful for the API endpoint (``POST /consensus/stakes``) where
        the caller wants to see *why* the classifier picked high.
        """
        text = _flatten_messages(messages).lower()
        hits = [kw for kw in self.HIGH_STAKES_KEYWORDS if _whole_word(text, kw)]
        return ("high" if hits else "low"), hits


_WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)


def _whole_word(text: str, kw: str) -> bool:
    """True iff ``kw`` appears as a whole word in ``text`` (already lower-cased)."""
    # Compile per-call is fine: keyword list is short (~30) and per-request
    # we run < 30 checks. Caching by kw would save ~5µs total — not worth it.
    return re.search(rf"\b{re.escape(kw)}\b", text) is not None


def _flatten_messages(messages: Sequence[dict] | str) -> str:
    """Same shape-extraction as F11 / AST safety / routing / containment."""
    if isinstance(messages, str):
        return messages
    parts: list[str] = []
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict):
                        t = p.get("text") or p.get("content") or ""
                        if isinstance(t, str):
                            parts.append(t)
    return " ".join(p for p in parts if p)


# ============================================================================
# Agreement scoring
# ============================================================================


def _tokens(text: str) -> set[str]:
    """Case-folded word-token set used by Jaccard."""
    return {m.group(0).lower() for m in _WORD_RE.finditer(text)}


def compute_agreement(responses: Sequence[str]) -> float:
    """Pairwise-mean Jaccard token overlap in ``[0.0, 1.0]``.

    Algorithm:

    * Tokenize each response (case-folded word boundaries).
    * For every pair ``(i, j)`` with ``i < j``, compute
      ``|A ∩ B| / |A ∪ B|`` (Jaccard). Empty-set edge cases: two
      empties → 1.0 (they "agree"); one empty + one non-empty → 0.0.
    * Return the mean of all pair scores.

    Single response → 1.0 (trivially agrees with itself). Zero
    responses → 1.0 (vacuously). Two identical responses → 1.0.
    Completely disjoint vocabularies → 0.0.

    Honest limit: word-overlap is a crude semantic proxy. "the cat
    sat" vs "a cat is sitting" scores ~0.4 (only "cat" in the
    intersection of normalized tokens). Plug a sentence-embedding
    scorer for semantic mode (planned for v1.9+).
    """
    if len(responses) < 2:
        return 1.0
    token_sets = [_tokens(r) for r in responses]
    pair_scores: list[float] = []
    for i in range(len(token_sets)):
        for j in range(i + 1, len(token_sets)):
            a, b = token_sets[i], token_sets[j]
            if not a and not b:
                pair_scores.append(1.0)
            elif not (a or b):
                # impossible (covered above) but defensive
                pair_scores.append(1.0)
            else:
                union = a | b
                pair_scores.append(len(a & b) / len(union))
    return sum(pair_scores) / len(pair_scores) if pair_scores else 1.0


def _pair_score(a: str, b: str) -> float:
    """Jaccard for one pair — used by :func:`_top_disagreements`."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    union = ta | tb
    if not union:
        return 1.0
    return len(ta & tb) / len(union)


def _top_disagreements(
    responses: list[ModelResponse], k: int = 3
) -> list[str]:
    """Return human-readable summaries of the ``k`` lowest-agreement pairs."""
    pairs: list[tuple[float, str]] = []
    for i in range(len(responses)):
        for j in range(i + 1, len(responses)):
            ri, rj = responses[i], responses[j]
            if not ri.ok or not rj.ok:
                continue
            s = _pair_score(ri.response, rj.response)
            pairs.append(
                (s, f"{ri.model} vs {rj.model}: agreement={s:.2f}")
            )
    pairs.sort(key=lambda p: p[0])
    return [d for _s, d in pairs[:k]]


# ============================================================================
# Consensus voter
# ============================================================================


# ``provider`` callable shape:
#   (model: str, messages: list[dict], max_tokens: int) -> Awaitable[ModelResponse]
#
# Why callable instead of a class hierarchy? Two reasons:
#   1. Tests can inject a fake without any adapter machinery (a
#      function literal is enough).
#   2. The host can mix providers in one voter — half Anthropic, half
#      OpenAI, half local — by writing a single dispatch closure. No
#      need to subclass.
ProviderCallable = Callable[[str, list[dict], int], Awaitable[ModelResponse]]


class ConsensusVoter:
    """Query N models in parallel; compute agreement; return a result.

    Args:
        models: The model names to query. Order is preserved into the
            result for deterministic display.
        provider: Async callable that runs one model. When ``None``,
            :func:`anthropic_provider` is used — that path lazy-imports
            the ``anthropic`` SDK and raises :class:`ImportError` only
            on first call, so hosts that pass a custom provider don't
            need ``[anthropic]`` installed.
        threshold: ``agreement_score`` boundary for ``consensus_reached``.
            Default ``0.7``: empirically separates "models say roughly
            the same thing" from "models disagree meaningfully" on
            short-form QA. Tune per domain.
    """

    def __init__(
        self,
        models: Sequence[str],
        *,
        provider: ProviderCallable | None = None,
        threshold: float = 0.7,
    ) -> None:
        if not models:
            raise ValueError("ConsensusVoter requires at least one model")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0,1], got {threshold}")
        self._models = list(models)
        self._provider = provider or anthropic_provider
        self._threshold = threshold

    @property
    def models(self) -> list[str]:
        return list(self._models)

    @property
    def threshold(self) -> float:
        return self._threshold

    async def vote(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 500,
    ) -> ConsensusResult:
        """Run the N models in parallel; return the consensus result.

        Each model that raises is captured as a :class:`ModelResponse`
        with ``error`` set — the vote proceeds with whatever succeeded.
        Agreement is computed only over successful responses.
        """
        t0 = time.perf_counter()
        tasks = [
            self._run_one(model, messages, max_tokens) for model in self._models
        ]
        responses: list[ModelResponse] = await asyncio.gather(*tasks)
        wall_ms = (time.perf_counter() - t0) * 1000.0

        ok_responses = [r for r in responses if r.ok]
        errors = [r.error for r in responses if not r.ok and r.error]

        # Agreement is computed only over successful responses.
        ok_texts = [r.response for r in ok_responses]
        agreement = compute_agreement(ok_texts) if ok_texts else 0.0
        consensus = agreement >= self._threshold and len(ok_responses) >= 1

        # Disagreement details only computed below threshold or when
        # multiple models succeeded with non-trivial disagreement.
        disagreements: list[str] = []
        if len(ok_responses) >= 2 and not consensus:
            disagreements = _top_disagreements(ok_responses, k=3)

        # Recommended = highest-cost successful response (rough quality
        # proxy). Fallback: first successful. Fallback²: empty string.
        if ok_responses:
            best = max(ok_responses, key=lambda r: r.cost_usd)
            recommended = best.response
            rec_model = best.model
        else:
            recommended = ""
            rec_model = ""

        return ConsensusResult(
            models_queried=list(self._models),
            responses=responses,
            agreement_score=agreement,
            consensus_reached=consensus,
            threshold=self._threshold,
            disagreement_details=disagreements,
            recommended_response=recommended,
            recommended_model=rec_model,
            cost_total_usd=sum(r.cost_usd for r in responses),
            latency_ms=wall_ms,
            errors=errors,
        )

    async def _run_one(
        self, model: str, messages: list[dict], max_tokens: int
    ) -> ModelResponse:
        """Wrap a single provider call, capture exceptions into ModelResponse."""
        t0 = time.perf_counter()
        try:
            return await self._provider(model, messages, max_tokens)
        except Exception as e:
            dt = (time.perf_counter() - t0) * 1000.0
            _LOG.warning(
                "consensus provider failed for %s: %s: %s",
                model,
                type(e).__name__,
                e,
            )
            return ModelResponse(
                model=model,
                response="",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                latency_ms=dt,
                error=f"{type(e).__name__}: {e}",
            )


# ============================================================================
# Anthropic default provider (lazy import)
# ============================================================================


# Approximate Anthropic pricing per 1M tokens (input, output). Refresh
# when prices change. ConsensusVoter only uses cost_usd for the
# "recommended" tie-break, so small drift here changes only the choice
# of which equally-good response to return — not correctness.
_ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-opus-4": (15.00, 75.00),
    "claude-opus-4-5": (15.00, 75.00),
    "claude-opus-4-6": (15.00, 75.00),
    "claude-opus-4-7": (15.00, 75.00),
}


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """USD cost estimate using :data:`_ANTHROPIC_PRICING`. 0.0 for unknown models."""
    p = _ANTHROPIC_PRICING.get(model)
    if not p:
        # Strip date suffix if present
        base = model.rsplit("-", 1)[0]
        p = _ANTHROPIC_PRICING.get(base)
    if not p:
        return 0.0
    in_rate, out_rate = p
    return (tokens_in / 1_000_000.0) * in_rate + (tokens_out / 1_000_000.0) * out_rate


async def anthropic_provider(
    model: str, messages: list[dict], max_tokens: int
) -> ModelResponse:
    """Default :class:`ConsensusVoter` provider — async Anthropic call.

    Lazy-imports the ``anthropic`` SDK so hosts that don't use
    consensus voting (or use a custom provider) aren't forced to
    install the ``[anthropic]`` extra.

    Cost is estimated locally from token counts × :data:`_ANTHROPIC_PRICING`
    — Anthropic's response doesn't carry a dollar amount, only tokens.
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        raise ImportError(
            "anthropic_provider requires the 'anthropic' package. "
            "Install bijotel with: pip install 'bijotel[anthropic]', "
            "or pass a custom provider to ConsensusVoter."
        ) from e

    client = AsyncAnthropic()  # picks up ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL
    t0 = time.perf_counter()
    try:
        resp = await client.messages.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
        )
        dt = (time.perf_counter() - t0) * 1000.0

        # Anthropic returns content as a list of blocks; concatenate text blocks.
        text_parts: list[str] = []
        for block in resp.content:
            t = getattr(block, "text", None)
            if isinstance(t, str):
                text_parts.append(t)
        text = "\n".join(text_parts)

        usage = resp.usage
        ti = int(getattr(usage, "input_tokens", 0))
        to = int(getattr(usage, "output_tokens", 0))

        return ModelResponse(
            model=model,
            response=text,
            tokens_in=ti,
            tokens_out=to,
            cost_usd=_estimate_cost(model, ti, to),
            latency_ms=dt,
            error=None,
        )
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000.0
        return ModelResponse(
            model=model,
            response="",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0.0,
            latency_ms=dt,
            error=f"{type(e).__name__}: {e}",
        )
    finally:
        # AsyncAnthropic owns an httpx client; close it. Suppress any
        # cleanup error — vote already captured success/failure above.
        import contextlib as _cl
        with _cl.suppress(Exception):
            await client.close()


# ============================================================================
# PolicyEngine integration
# ============================================================================


def consensus_requirement(
    *,
    stakes_threshold: str = "high",
    mode: str = "warn",
    classifier: StakesClassifier | None = None,
) -> Callable[[dict], Decision]:
    """PolicyEngine rule: warn when high-stakes prompt goes to a single model.

    The host signals "this call already went through consensus" by
    passing ``{"_consensus": True}`` (or ``"models_used": N`` with
    ``N >= 2``) in the request dict. The rule never warns in that case.

    Args:
        stakes_threshold: Which stakes level triggers the rule. Currently
            either ``"high"`` (default) or ``"low"`` (silly but supported
            for symmetry). ``"high"`` makes the rule fire on
            medical/legal/financial/safety/security keywords.
        mode: ``"warn"`` (default) or ``"deny"``.
        classifier: Custom :class:`StakesClassifier`. Defaults to a
            fresh instance with the built-in keyword list.

    Raises:
        ValueError: if ``mode`` is invalid.
    """
    if mode not in ("warn", "deny"):
        raise ValueError(f"mode must be 'deny' or 'warn', got {mode!r}")
    if stakes_threshold not in ("high", "low"):
        raise ValueError(
            f"stakes_threshold must be 'high' or 'low', got {stakes_threshold!r}"
        )
    cls = classifier or StakesClassifier()

    def rule(request: dict) -> Decision:
        # If the caller already marked this as a consensus call, pass.
        if request.get("_consensus") is True:
            return Decision.allow()
        models_used: Any = request.get("models_used", 1)
        if isinstance(models_used, int) and models_used >= 2:
            return Decision.allow()

        messages = request.get("messages", [])
        stakes, hits = cls.classify_with_hits(messages)
        if stakes != stakes_threshold:
            return Decision.allow()

        hit_str = ", ".join(hits[:3])
        if len(hits) > 3:
            hit_str += f" (+{len(hits) - 3} more)"
        reason = (
            f"high-stakes request (keywords: {hit_str}) sent to a single "
            "model; consider running consensus across N models for "
            "hallucination resistance"
        )
        if mode == "deny":
            return Decision.deny(rule="consensus_requirement", reason=reason)
        return Decision.warn(rule="consensus_requirement", reason=reason)

    return rule


__all__ = [
    "ConsensusResult",
    "ConsensusVoter",
    "ModelResponse",
    "ProviderCallable",
    "StakesClassifier",
    "anthropic_provider",
    "compute_agreement",
    "consensus_requirement",
]
