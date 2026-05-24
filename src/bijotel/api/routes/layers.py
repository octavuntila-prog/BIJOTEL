"""``/layers`` — report status of every BIJOTEL bijuterie covered.

This is the public "what does this BIJOTEL install actually do?" surface.
The dashboard (v1.2.0) drives a single card per row.

Status semantics (kept honest — M2 reality > docs):

* **active** — the layer is wired in this process *right now*. For the
  HMAC chain / CAS / DAG: chain.db exists and the corresponding table is
  populated. For policy: a ``PolicyEngine`` is attached. For regression:
  default detector path resolves.
* **available** — code is importable and the layer can be activated by
  the host, but nothing in this server process is currently using it.
  Importable usually requires the right extras: ``fingerprint`` for
  ``sentence-transformers``, ``ast`` for ``tree-sitter`` etc.
* **planned** — bijuterie tracked in the catalog but no code shipped
  yet (Energy #3, Consensus #9, …).

The endpoint never raises on import failures — missing extras become
``status="available"`` with a ``note`` explaining what to install.

Provenance: BIJOTEL-original, v1.1.0.
"""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from bijotel.api.models import LayersResponse, LayerStatus

router = APIRouter(prefix="/layers", tags=["layers"])


def _has_module(dotted: str) -> bool:
    """True iff ``dotted`` can be imported (no exception, no side-effects beyond import)."""
    try:
        importlib.import_module(dotted)
        return True
    except Exception:
        return False


def _table_row_count(db_path: Path, table: str) -> int | None:
    """Return ``COUNT(*)`` for ``table`` in ``db_path``, or ``None`` if absent."""
    if not db_path.is_file():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return None


def _engine_has_rule(engine: Any, factory_name: str) -> bool:
    """True iff ``engine`` has a rule produced by ``factory_name``.

    Each rule factory in :mod:`bijotel.policy` and :mod:`bijotel.layers`
    returns a closure named ``rule`` defined inside the factory. The
    closure's ``__qualname__`` therefore contains the factory name,
    e.g. ``routing_recommendation.<locals>.rule``. We use that as a
    detection sentinel — no factory needs to set an explicit flag.

    Returns False for ``engine is None``, missing ``_rules``, or any
    inspection error (defensive: this endpoint must never raise).
    """
    if engine is None:
        return False
    try:
        rules = getattr(engine, "_rules", []) or []
        for rule in rules:
            qn = getattr(rule, "__qualname__", "") or ""
            if factory_name in qn:
                return True
    except Exception:
        pass
    return False


def _fingerprint_db_active(db_path: Path) -> bool:
    """True iff the sibling ``bijotel_fingerprints.db`` exists & has entries.

    ``FingerprintSpanProcessor`` writes to a dedicated DB alongside the
    chain. Presence of the file alone isn't enough (it may be created
    empty); requires at least one entry to count as active.
    """
    fp_path = db_path.parent / "bijotel_fingerprints.db"
    n = _table_row_count(fp_path, "fingerprints")
    return bool(n and n > 0)


def _containment_active(request: Request) -> bool:
    """True iff a ContainmentGuard is bound on app state.

    Either:
      * host passed ``containment_guard=`` to ``create_app``, OR
      * a previous ``POST /containment/evaluate`` call lazy-built one
        (cached on state per :mod:`bijotel.api.routes.containment`).

    Both cases prove the endpoint is operable on this process.
    """
    return getattr(request.app.state, "containment_guard", None) is not None


def _build_layers(request: Request) -> list[LayerStatus]:
    """Compute the 13-layer manifest for this server's current state."""
    db_path = Path(request.app.state.db_path)
    engine = getattr(request.app.state, "policy_engine", None)

    chain_count = _table_row_count(db_path, "chain")
    cas_count = _table_row_count(db_path, "cas")
    dag_count = _table_row_count(db_path, "dag_nodes")
    fingerprint_active = _fingerprint_db_active(db_path)
    ast_in_engine = _engine_has_rule(engine, "ast_safety_check")
    routing_in_engine = _engine_has_rule(engine, "routing_recommendation")
    containment_active = _containment_active(request)

    layers: list[LayerStatus] = []

    # --- ACTIVE / AVAILABLE depending on runtime state ---

    layers.append(
        LayerStatus(
            id="forensic_chain",
            bijuterie="#11",
            status="active" if chain_count and chain_count > 0 else "available",
            metrics={"entries": chain_count or 0},
        )
    )

    layers.append(
        LayerStatus(
            id="content_addressable",
            bijuterie="#2",
            status="active" if cas_count and cas_count > 0 else "available",
            metrics={"cas_entries": cas_count or 0},
        )
    )

    layers.append(
        LayerStatus(
            id="merkle_dag",
            bijuterie="#2",
            status="active" if dag_count and dag_count > 0 else "available",
            metrics={"dag_nodes": dag_count or 0},
            note="dag_refs index for inbound lookup",
        )
    )

    layers.append(
        LayerStatus(
            id="policy_gate",
            bijuterie="#10",
            status="active" if engine is not None else "available",
            metrics={
                "rules": len(getattr(engine, "_rules", [])) if engine else 0,
            },
        )
    )

    # --- AVAILABLE: code ships, extras may be missing ---

    fp_have = _has_module("sentence_transformers")
    layers.append(
        LayerStatus(
            id="fingerprint",
            bijuterie="#7",
            # Active iff the sibling fingerprints.db has at least one
            # row — the FingerprintSpanProcessor is wired and emitting.
            # Available otherwise (code ships, just nothing written yet).
            status="active" if fingerprint_active else "available",
            note=None if fp_have else "Install 'bijotel[fingerprint]' for semantic mode.",
            metrics={
                "sentence_transformers": fp_have,
                "deterministic_fingerprints": fingerprint_active,
            },
        )
    )

    ast_have = _has_module("tree_sitter")
    layers.append(
        LayerStatus(
            id="ast_safety",
            bijuterie="#5",
            # Active iff this server's PolicyEngine actually has an
            # ast_safety_check rule wired (not just the import being
            # available). Mirrors the routing detection below.
            status="active" if ast_in_engine else "available",
            note=None if ast_have else "Install 'bijotel[ast]' for bash AST scan.",
            metrics={
                "tree_sitter": ast_have,
                "wired_in_engine": ast_in_engine,
            },
        )
    )

    layers.append(
        LayerStatus(
            id="routing",
            bijuterie="#15",
            # Active iff this server's PolicyEngine actually has a
            # routing_recommendation rule wired. Detection via the
            # rule closure's __qualname__ — see _engine_has_rule.
            status="active" if routing_in_engine else "available",
            note="Pareto cost/quality/latency + Budget (SQLite per-agent).",
            metrics={"wired_in_engine": routing_in_engine},
        )
    )

    layers.append(
        LayerStatus(
            id="misalignment",
            bijuterie="#18",
            status="available",
            note="29 probes across 8 attack categories.",
        )
    )

    layers.append(
        LayerStatus(
            id="containment",
            bijuterie="Combo D",
            # Active iff a ContainmentGuard is on app state (either
            # host-supplied or lazy-built by the /containment/evaluate
            # handler on first call). Both prove the layer is operable.
            status="active" if containment_active else "available",
            note="Permitted + Safe + Sealed orchestrator.",
            metrics={"guard_attached": containment_active},
        )
    )

    layers.append(
        LayerStatus(
            id="regression",
            bijuterie="#16",
            status="active" if chain_count and chain_count >= 5 else "available",
            metrics={
                "min_baseline_samples": 5,
                "ready": bool(chain_count and chain_count >= 5),
            },
            note="z-score + IQR over input_tokens / output_tokens / cost.",
        )
    )

    layers.append(
        LayerStatus(
            id="otel_genai",
            bijuterie="#19",
            status="active",
            note="OpenTelemetry GenAI semantic conventions used throughout.",
        )
    )

    layers.append(
        LayerStatus(
            id="provider_protocol",
            bijuterie="#7",
            status="active",
            metrics={
                "adapters": ["anthropic", "openai"],
            },
        )
    )

    # --- AVAILABLE: consensus is shipped code (v1.8.0) — flips to active
    #     when host attaches a provider on app.state ---

    consensus_active = (
        getattr(request.app.state, "consensus_provider", None) is not None
    )
    layers.append(
        LayerStatus(
            id="consensus",
            bijuterie="#9",
            # Active iff the host wired a provider OR the endpoint has
            # been called (the route doesn't lazy-build a provider —
            # default uses anthropic SDK which is the host's choice).
            status="active" if consensus_active else "available",
            note="N-model voting + StakesClassifier gate (v1.8.0).",
            metrics={"provider_attached": consensus_active},
        )
    )

    # --- AVAILABLE: energy is shipped code (v1.9.0). Flips to active
    #     when the host has an EnergyTracker on app state, OR when the
    #     chain DB has an energy_log table with rows (proxy: backfill
    #     was run, or the host wired EnergySpanProcessor). ---

    energy_active = (
        getattr(request.app.state, "energy_tracker", None) is not None
        or (_table_row_count(db_path, "energy_log") or 0) > 0
    )
    energy_rows = _table_row_count(db_path, "energy_log") or 0
    layers.append(
        LayerStatus(
            id="energy",
            bijuterie="#3",
            status="active" if energy_active else "available",
            note="Wh + gCO2 per LLM call (v1.9.0).",
            metrics={"energy_log_rows": energy_rows},
        )
    )

    return layers


@router.get(
    "",
    response_model=LayersResponse,
    summary="Status of every bijuterie covered by this BIJOTEL build",
)
def layers_list(request: Request) -> LayersResponse:
    """Return the layer manifest with per-layer counters."""
    layers = _build_layers(request)

    def count(status: str) -> int:
        return sum(1 for layer in layers if layer.status == status)

    return LayersResponse(
        layers=layers,
        total=len(layers),
        active=count("active"),
        available=count("available"),
        planned=count("planned"),
    )


__all__ = ["router"]


# Type-checker happiness for Any reference
_ = Any
