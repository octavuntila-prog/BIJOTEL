"""``/consensus/*`` — Multi-LLM voting endpoints (Bijuteria #9).

* ``POST /consensus/evaluate`` — fire N parallel model calls, compute
  agreement, return the consensus result.
* ``POST /consensus/stakes`` — classify a prompt as high- or low-stakes
  (no LLM calls).

The evaluate endpoint needs a model provider. Resolution order:

1. ``app.state.consensus_provider`` — host-supplied callable. Use it.
2. Default :func:`bijotel.layers.consensus.anthropic_provider`. That
   path lazy-imports the ``anthropic`` SDK and reads
   ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` from the environment.
   If the SDK isn't installed, the endpoint returns ``503``.

Provenance: BIJOTEL-original, v1.8.0.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from bijotel.api.models import (
    ConsensusEvaluateRequest,
    ConsensusEvaluateResponse,
    ConsensusModelResponse,
    ConsensusStakesRequest,
    ConsensusStakesResponse,
)
from bijotel.layers.consensus import (
    ConsensusVoter,
    StakesClassifier,
)

router = APIRouter(prefix="/consensus", tags=["consensus"])


def _resolve_provider(request: Request):
    """Return the consensus provider callable on app state, or None for default."""
    return getattr(request.app.state, "consensus_provider", None)


@router.post(
    "/evaluate",
    response_model=ConsensusEvaluateResponse,
    summary="Run an N-model consensus vote on the same prompt.",
)
def consensus_evaluate(
    payload: ConsensusEvaluateRequest, request: Request
) -> ConsensusEvaluateResponse:
    """Fire N parallel calls; return :class:`ConsensusResult` as JSON.

    Hosts that don't want the default :func:`anthropic_provider`
    (because they don't have the ``[anthropic]`` extra installed, or
    they want to mix providers) can attach
    ``app.state.consensus_provider`` to a custom async callable
    matching :data:`ProviderCallable`.

    Returns 503 when neither a custom provider is wired nor the
    default ``anthropic`` SDK is importable — the failure mode is
    surfaced immediately rather than as N HTTP timeouts.
    """
    provider = _resolve_provider(request)

    # If no custom provider, verify the default's deps are present so
    # we fail fast with a clear 503 instead of N obscure errors.
    if provider is None:
        try:
            import anthropic  # noqa: F401
        except ImportError as e:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No consensus_provider attached and the default "
                    "anthropic_provider requires the 'anthropic' SDK. "
                    "Install with: pip install 'bijotel[anthropic]' or "
                    "attach app.state.consensus_provider to a custom async "
                    "callable."
                ),
            ) from e
        # Anthropic SDK is importable — cache the default provider on
        # app state so /api/layers reports `consensus` as "active" and
        # the next call hits the same callable (no re-import cost).
        from bijotel.layers.consensus import anthropic_provider

        request.app.state.consensus_provider = anthropic_provider
        provider = anthropic_provider

    voter = ConsensusVoter(
        models=payload.models,
        provider=provider,  # None falls back to anthropic_provider
        threshold=payload.threshold,
    )

    # FastAPI handler runs sync but we need an asyncio event loop. The
    # endpoint is a sync def to keep the codebase consistent with the
    # other routes (chain/policy/regression) — they're all sync too.
    # asyncio.run is safe here: each request is its own ephemeral loop.
    result = asyncio.run(
        voter.vote(messages=payload.messages, max_tokens=payload.max_tokens)
    )

    return ConsensusEvaluateResponse(
        models_queried=result.models_queried,
        responses=[
            ConsensusModelResponse(
                model=r.model,
                response=r.response,
                tokens_in=r.tokens_in,
                tokens_out=r.tokens_out,
                cost_usd=r.cost_usd,
                latency_ms=r.latency_ms,
                error=r.error,
            )
            for r in result.responses
        ],
        agreement_score=result.agreement_score,
        consensus_reached=result.consensus_reached,
        threshold=result.threshold,
        disagreement_details=result.disagreement_details,
        recommended_response=result.recommended_response,
        recommended_model=result.recommended_model,
        cost_total_usd=result.cost_total_usd,
        latency_ms=result.latency_ms,
        errors=result.errors,
    )


@router.post(
    "/stakes",
    response_model=ConsensusStakesResponse,
    summary="Classify a prompt as high- or low-stakes (no LLM calls).",
)
def consensus_stakes(
    payload: ConsensusStakesRequest, request: Request  # noqa: ARG001
) -> ConsensusStakesResponse:
    """Run the host's :class:`StakesClassifier` over ``messages``.

    Useful for upstream gating: only route high-stakes prompts through
    the (expensive) ``/consensus/evaluate`` endpoint; low-stakes go to
    a single model unchanged. Stateless, no network calls.
    """
    sc = StakesClassifier()
    stakes, hits = sc.classify_with_hits(payload.messages)
    return ConsensusStakesResponse(stakes=stakes, keywords_found=hits)


__all__ = ["router"]
