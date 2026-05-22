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


def _build_layers(request: Request) -> list[LayerStatus]:
    """Compute the 13-layer manifest for this server's current state."""
    db_path = Path(request.app.state.db_path)
    engine = getattr(request.app.state, "policy_engine", None)

    chain_count = _table_row_count(db_path, "chain")
    cas_count = _table_row_count(db_path, "cas")
    dag_count = _table_row_count(db_path, "dag_nodes")

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
            status="available",
            note=None if fp_have else "Install 'bijotel[fingerprint]' for semantic mode.",
            metrics={"sentence_transformers": fp_have},
        )
    )

    ast_have = _has_module("tree_sitter")
    layers.append(
        LayerStatus(
            id="ast_safety",
            bijuterie="#5",
            status="available",
            note=None if ast_have else "Install 'bijotel[ast]' for bash AST scan.",
            metrics={"tree_sitter": ast_have},
        )
    )

    layers.append(
        LayerStatus(
            id="routing",
            bijuterie="#15",
            status="available",
            note="Pareto cost/quality/latency + Budget (SQLite per-agent).",
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
            status="available",
            note="Permitted + Safe + Sealed orchestrator.",
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

    # --- PLANNED ---

    layers.append(
        LayerStatus(
            id="energy",
            bijuterie="#3",
            status="planned",
            note="Energy accounting per call. v1.3.0 target.",
        )
    )

    layers.append(
        LayerStatus(
            id="consensus",
            bijuterie="#9",
            status="planned",
            note="Multi-model consensus voting. v1.3.0 target.",
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
