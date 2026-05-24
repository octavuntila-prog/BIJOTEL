"""``/energy/*`` — AI energy + carbon accounting (Bijuteria #3).

* ``POST /energy/estimate`` — stateless: given a (model, tokens_in,
  tokens_out, region), return Wh + grams CO2. No persistence.
* ``GET  /energy/summary``  — aggregate over the host's
  :class:`EnergyTracker` (db_path same as chain.db). Filterable by
  ``since`` / ``until`` (ISO-8601) and ``agent_id``.

The summary endpoint reads from the host-attached tracker:
``app.state.energy_tracker``. When ``None``, the route lazy-builds
one against the chain DB so single-call hosts get a working answer
out of the box (``us-east`` defaults).

Provenance: BIJOTEL-original, v1.9.0.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from bijotel.api.models import (
    EnergyAgentEntry,
    EnergyEstimateRequest,
    EnergyEstimateResponse,
    EnergyModelEntry,
    EnergySummaryResponse,
)
from bijotel.layers.energy import (
    CarbonCalculator,
    EnergyEstimator,
    EnergyTracker,
)

router = APIRouter(prefix="/energy", tags=["energy"])


def _resolve_tracker(request: Request) -> EnergyTracker:
    """Return the EnergyTracker for this app, building lazily if needed."""
    t = getattr(request.app.state, "energy_tracker", None)
    if t is not None:
        return t

    db_path = getattr(request.app.state, "db_path", None)
    if not db_path:
        raise HTTPException(
            status_code=503,
            detail=(
                "No EnergyTracker configured and app.state.db_path is "
                "missing. Pass a db_path to create_app() or attach "
                "an EnergyTracker at app.state.energy_tracker."
            ),
        )
    tracker = EnergyTracker(db_path)
    request.app.state.energy_tracker = tracker
    return tracker


@router.post(
    "/estimate",
    response_model=EnergyEstimateResponse,
    summary="Estimate Wh + CO2 for a single call (stateless).",
)
def energy_estimate(payload: EnergyEstimateRequest) -> EnergyEstimateResponse:
    """Pure tokens→Wh→CO2 math; no DB write."""
    est = EnergyEstimator()
    calc = CarbonCalculator(payload.region) if payload.region else CarbonCalculator()
    wh = est.estimate_wh(payload.model, payload.tokens_in, payload.tokens_out)
    co2 = calc.wh_to_co2_grams(wh)
    return EnergyEstimateResponse(
        model=payload.model,
        tokens=payload.tokens_in + payload.tokens_out,
        wh=wh,
        co2_grams=co2,
        region=calc.region,
        intensity_g_per_kwh=calc.intensity_g_per_kwh,
    )


@router.get(
    "/summary",
    response_model=EnergySummaryResponse,
    summary="Aggregate energy + carbon over a window.",
)
def energy_summary(
    request: Request,
    since: str | None = Query(
        None, description="ISO-8601 lower bound, inclusive."
    ),
    until: str | None = Query(
        None, description="ISO-8601 upper bound, exclusive."
    ),
    agent_id: str | None = Query(None, description="Filter to one agent."),
) -> EnergySummaryResponse:
    """Read from the host's EnergyTracker (db_path same as chain.db)."""
    tracker = _resolve_tracker(request)
    s = tracker.summary(since=since, until=until, agent_id=agent_id)
    return EnergySummaryResponse(
        total_calls=s.total_calls,
        total_tokens=s.total_tokens,
        total_wh=s.total_wh,
        total_co2_grams=s.total_co2_grams,
        co2_kg=s.co2_kg,
        equivalent_km_driven=s.equivalent_km_driven,
        equivalent_phone_charges=s.equivalent_phone_charges,
        equivalent_kettle_boils=s.equivalent_kettle_boils,
        per_model=[
            EnergyModelEntry(
                model=m.model, calls=m.calls, tokens=m.tokens,
                wh=m.wh, co2_grams=m.co2_grams,
            )
            for m in s.per_model.values()
        ],
        per_agent=[
            EnergyAgentEntry(
                agent_id=a.agent_id, calls=a.calls, tokens=a.tokens,
                wh=a.wh, co2_grams=a.co2_grams,
            )
            for a in s.per_agent.values()
        ],
        since=s.since,
        until=s.until,
        has_data=s.has_data,
    )


__all__ = ["router"]
