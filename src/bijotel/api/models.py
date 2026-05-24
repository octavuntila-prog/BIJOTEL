"""Pydantic response models for BIJOTEL API endpoints.

Centralized here so:
* route handlers and tests share one source of truth on shapes
* OpenAPI schema (served at ``/docs``) is precise and self-documenting
* downstream clients (the v1.2.0 React dashboard) can generate typed
  bindings via ``openapi-typescript`` against ``/openapi.json``

Each model carries an explicit docstring; FastAPI surfaces it in the
generated OpenAPI under ``schema.description``.

Provenance: BIJOTEL-original, v1.0.0+.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ───────────────────────── /chain ─────────────────────────


class ChainEntrySummary(BaseModel):
    """One row in ``GET /chain`` paginated list (lightweight)."""

    seq: int = Field(..., description="Monotonic chain sequence number.")
    timestamp: str = Field(
        ..., description="ISO-8601 UTC of span end time."
    )
    trace_id: str = Field(..., description="32-hex OTel trace id.")
    span_id: str = Field(..., description="16-hex OTel span id.")
    span_name: str = Field(..., description="Span name (e.g. anthropic.chat).")
    span_kind: str | None = Field(
        None, description="OTel span kind (CLIENT/INTERNAL/...) or null."
    )
    canonical_hash: str = Field(
        ..., description="SHA-256 of canonical body, hex."
    )
    hmac_valid: bool = Field(
        ..., description="True iff the row's HMAC matches the chain reconstruction."
    )


class Pagination(BaseModel):
    """Standard pagination envelope echoed back to the caller."""

    limit: int
    offset: int
    has_more: bool


class ChainListResponse(BaseModel):
    """Body of ``GET /chain``."""

    total: int = Field(..., description="Total rows in the chain table.")
    entries: list[ChainEntrySummary]
    pagination: Pagination


class ChainEntryDetail(BaseModel):
    """Body of ``GET /chain/{seq}``."""

    seq: int
    timestamp: str
    trace_id: str
    span_id: str
    span_name: str
    span_kind: str | None
    canonical_body: dict[str, Any] = Field(
        ...,
        description="Parsed canonical body (JSON object). Includes name/kind/"
        "attributes/events/resource/scope/status/start_time_ns/end_time_ns.",
    )
    canonical_hash: str
    prev_hash: str
    hmac_hash: str
    hmac_valid: bool
    cas_ref: str | None = Field(
        None, description="semantic_body_hash (CAS lookup key) or null."
    )


class ChainVerifyRequest(BaseModel):
    """Body of ``POST /chain/verify``."""

    full: bool = Field(
        False,
        description="If true, recompute every row's HMAC and verify the full "
        "chain. If false, only verify the last row's prev_hash matches the "
        "previous row's hmac_hash (fast smoke check).",
    )


class ChainVerifyResponse(BaseModel):
    """Body of ``POST /chain/verify`` reply."""

    valid: bool
    entries_verified: int
    first_seq: int | None
    last_seq: int | None
    error: str | None = Field(
        None,
        description="Human-readable reason if valid=false. None on success.",
    )
    error_seq: int | None = Field(
        None,
        description="Sequence number where verification failed, or null.",
    )


class ChainStatsResponse(BaseModel):
    """Body of ``GET /chain/stats``."""

    total_entries: int
    cas_entries: int
    dedup_factor: float = Field(
        ...,
        description=(
            "Refs per unique CAS body. 1.0 = no dedup; higher = more reuse."
        ),
    )
    first_entry: str | None = Field(
        None, description="ISO-8601 UTC of earliest entry, or null if empty."
    )
    last_entry: str | None = Field(
        None, description="ISO-8601 UTC of latest entry, or null if empty."
    )
    age_days: float = Field(
        ..., description="Span from first to last entry in days."
    )
    db_size_bytes: int = Field(
        ..., description="On-disk size of the chain SQLite file (bytes)."
    )
    entries_per_day: float = Field(
        ..., description="total_entries / age_days; 0 when age_days==0."
    )


# ───────────────────────── /policy ─────────────────────────


class PolicyRuleSummary(BaseModel):
    """One row in ``GET /policy/rules``."""

    name: str = Field(..., description="Rule factory function name.")
    mode: str | None = Field(
        None,
        description="warn / deny — extracted from the rule's closure if available.",
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Best-effort introspection (pattern count, limits, etc.).",
    )


class PolicyRulesResponse(BaseModel):
    """Body of ``GET /policy/rules``."""

    rules: list[PolicyRuleSummary]
    total: int


class PolicyEvaluateRequest(BaseModel):
    """Body of ``POST /policy/evaluate`` (mirrors the engine's request shape)."""

    messages: list[dict[str, Any]] = Field(
        ...,
        description="OTel GenAI-style messages list. Each entry: "
        "{'role': str, 'content': str | list}.",
    )
    model: str | None = Field(
        None, description="Optional gen_ai.request.model used by some rules."
    )
    max_tokens: int | None = Field(
        None, description="Optional max_tokens used by output_length_limit."
    )


class PolicyWarning(BaseModel):
    """One warning emitted by the engine (mode='warn' rules)."""

    rule: str
    reason: str


class PolicyEvaluateResponse(BaseModel):
    """Body of ``POST /policy/evaluate``."""

    decision: str = Field(
        ..., description="One of: 'allow', 'deny'. Warnings live in their own list."
    )
    denied: bool
    deny_rule: str | None = Field(
        None, description="Rule name that denied the call, if any."
    )
    deny_reason: str | None = Field(
        None, description="Human-readable reason behind the deny, if any."
    )
    warnings: list[PolicyWarning]
    evaluation_ms: float = Field(
        ..., description="Wall-clock duration of PolicyEngine.evaluate() in ms."
    )


# ───────────────────────── /layers ─────────────────────────


class LayerStatus(BaseModel):
    """One bijuterie entry in ``GET /layers``."""

    id: str = Field(..., description="Stable machine id (snake_case).")
    bijuterie: str = Field(
        ..., description="Catalog reference (#1..#20 or 'Combo D')."
    )
    status: str = Field(
        ..., description="'active' / 'available' / 'planned'."
    )
    note: str | None = Field(
        None, description="Free-text qualifier (e.g. 'requires [ast] extra')."
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        description="Layer-specific counters (entries, runs, cache size, ...).",
    )


class LayersResponse(BaseModel):
    """Body of ``GET /layers``."""

    layers: list[LayerStatus]
    total: int
    active: int
    available: int
    planned: int


# ───────────────────────── /regression ─────────────────────────


class AnomalyDetail(BaseModel):
    """One anomaly detected in a single dimension."""

    dimension: str = Field(..., description="input_tokens / output_tokens / cost.")
    seq: int = Field(..., description="Chain seq where the anomaly fired.")
    timestamp: str = Field(..., description="ISO-8601 UTC.")
    value: float = Field(..., description="Observed value at the span.")
    baseline_mean: float = Field(..., description="Mean of the baseline window.")
    z_score: float | None = Field(
        None,
        description="z-score against baseline; null when baseline stdev is 0.",
    )
    iqr_distance: float | None = Field(
        None, description="Tukey IQR distance; null when IQR is 0."
    )
    method_triggered: str = Field(
        ..., description="'z_score' / 'iqr' / 'both' — which detection fired."
    )
    severity: str = Field(
        ..., description="'warning' (single method) or 'anomaly' (both)."
    )


class RegressionDimensionResult(BaseModel):
    """Per-dimension result inside a regression run."""

    baseline_mean: float | None = Field(
        None, description="null when not enough baseline data."
    )
    baseline_std: float | None = Field(
        None, description="null when not enough baseline data."
    )
    samples: int = Field(..., description="Spans evaluated against baseline.")
    anomalies: int = Field(..., description="Anomaly count for this dimension.")
    status: str = Field(
        ...,
        description="'clean' (0 anomalies) / 'anomaly' (>=1) / 'insufficient_data'.",
    )


class RegressionRunResponse(BaseModel):
    """Body of ``GET /regression/latest`` and ``POST /regression/run``."""

    run_id: int | None = Field(
        None,
        description="Persisted regression_runs.id when from history; null on "
        "synchronous run when ``persist=false`` is used.",
    )
    timestamp: str = Field(..., description="When the run was executed (ISO-8601 UTC).")
    window: int = Field(..., description="Baseline window size used.")
    z_threshold: float = Field(..., description="z-score threshold used.")
    dimensions: dict[str, RegressionDimensionResult] = Field(
        ..., description="Keyed by dimension name."
    )
    details: list[AnomalyDetail] = Field(
        default_factory=list,
        description="All anomaly records across dimensions (flat list).",
    )
    total_anomalies: int
    status: str = Field(..., description="'clean' / 'anomaly' / 'insufficient_data'.")


class RegressionHistoryEntry(BaseModel):
    """One row in ``GET /regression/history``."""

    run_id: int
    timestamp: str
    window: int
    total_anomalies: int
    status: str


class RegressionHistoryResponse(BaseModel):
    """Body of ``GET /regression/history``."""

    runs: list[RegressionHistoryEntry]
    total_runs: int


class RegressionRunRequest(BaseModel):
    """Body of ``POST /regression/run``."""

    window: int = Field(100, ge=5, le=10000, description="Baseline window size.")
    z_threshold: float = Field(
        3.0, gt=0, le=10.0, description="z-score absolute threshold for anomaly."
    )
    filter_model: str | None = Field(
        None, description="Optional gen_ai.request.model exact-match filter."
    )
    persist: bool = Field(
        True,
        description="If true, the run is written to the regression_runs table and "
        "becomes visible at /regression/history. False = ad-hoc dry-run.",
    )


# ───────────────────────── /containment ─────────────────────────


class ContainmentEvaluateRequest(BaseModel):
    """Body of ``POST /containment/evaluate`` — same shape as policy/evaluate."""

    messages: list[dict[str, Any]] = Field(
        ...,
        description="OTel GenAI-style messages list. Each entry: "
        "{'role': str, 'content': str | list}. Code blocks inside "
        "content will be picked up by the AST checker if one is wired.",
    )
    model: str | None = Field(
        None, description="Optional gen_ai.request.model used by some rules."
    )
    max_tokens: int | None = Field(
        None, description="Optional max_tokens used by output_length_limit."
    )
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form extra fields preserved into the seal record "
        "for forensic value (agent_id, request_id, tool_name, ...).",
    )


class ASTViolationItem(BaseModel):
    """One AST safety violation surfaced by the containment scan."""

    pattern: str = Field(..., description="Pattern name (e.g. 'dangerous_rm').")
    language: str = Field(..., description="'bash' / 'python'.")
    line: int | None = Field(None, description="Line number within the code block.")
    severity: str = Field(..., description="'critical' / 'high' / 'medium' / 'low'.")


class ContainmentEvaluateResponse(BaseModel):
    """Body of ``POST /containment/evaluate`` — three-question result.

    Mirrors :class:`bijotel.layers.containment.ContainmentDecision`
    flattened for JSON transport. The dashboard reads ``permitted`` +
    ``safe`` + ``sealed`` as three status pills (green/red).
    """

    permitted: bool = Field(
        ..., description="PolicyEngine: allow OR warn (not deny). The 'is it permitted?' answer."
    )
    safe: bool = Field(
        ...,
        description="AST: no critical violations (or no AST checker wired). "
        "The 'is it safe?' answer.",
    )
    sealed: bool | None = Field(
        None,
        description="Chain: containment record written. ``null`` if no "
        "chain_writer is wired (decision produced, not persisted). "
        "The 'was it noted?' answer.",
    )
    all_clear: bool = Field(
        ...,
        description="``permitted AND safe`` — the host's go/no-go signal "
        "(sealing is informational, not blocking).",
    )
    policy_decision: str = Field(
        ..., description="'allow' / 'warn' / 'deny' — the aggregate engine state."
    )
    policy_warnings: list[PolicyWarning] = Field(
        default_factory=list,
        description="All warn-mode rules that matched, even on permit.",
    )
    ast_violations: list[ASTViolationItem] = Field(
        default_factory=list,
        description="AST scan findings. Empty when no code blocks or no checker.",
    )
    seal_record: dict[str, Any] = Field(
        default_factory=dict,
        description="Canonical JSON suitable for writing into an audit chain "
        "(host writes it via their own processor — this endpoint only "
        "produces it). Includes the action's keys for forensic context.",
    )
    evaluation_ms: float = Field(
        ..., description="Wall-clock duration of guard.evaluate_action() in ms."
    )


# ───────────────────────── /export ─────────────────────────


class ExportVerifyResponse(BaseModel):
    """Body of ``POST /export/verify``."""

    valid: bool
    reason: str | None = Field(
        None,
        description="null on success; on failure, human-readable explanation "
        "(e.g. 'chain_signature mismatch', 'hmac_hash mismatch at seq=42').",
    )
    entries_count: int | None = Field(
        None,
        description="Number of entries declared in the export envelope. Reported "
        "even on failure when the value could be parsed.",
    )
    head_hash: str | None = Field(
        None, description="Last hmac_hash in the chain export."
    )
    format: str | None = Field(
        None, description="Export schema version, e.g. 'bijotel-chain-v1'."
    )
