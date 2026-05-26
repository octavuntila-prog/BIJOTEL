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
    # v2.3.0: range filters mirror the CLI flags. When any is set,
    # `full` is implied (we always do a per-row HMAC recompute over
    # the slice — there's no smoke shortcut for ranges).
    seq_start: int | None = Field(
        None,
        description="Inclusive lower bound on seq. v2.3.0+.",
    )
    seq_end: int | None = Field(
        None,
        description="Inclusive upper bound on seq. v2.3.0+.",
    )
    since_ns: int | None = Field(
        None,
        description="Inclusive lower bound on timestamp_ns. v2.3.0+.",
    )
    until_ns: int | None = Field(
        None,
        description="Inclusive upper bound on timestamp_ns. v2.3.0+.",
    )
    last_n: int | None = Field(
        None,
        description="Verify only the last N entries by seq. v2.3.0+.",
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


# ───────────────────────── /energy ─────────────────────────


class EnergyEstimateRequest(BaseModel):
    """Body of ``POST /energy/estimate``."""

    model: str = Field(..., description="LLM model name.")
    tokens_in: int = Field(..., ge=0)
    tokens_out: int = Field(..., ge=0)
    region: str | None = Field(
        None,
        description="Optional override of the host's default grid region.",
    )


class EnergyEstimateResponse(BaseModel):
    """Body of ``POST /energy/estimate``."""

    model: str
    tokens: int = Field(..., description="tokens_in + tokens_out.")
    wh: float
    co2_grams: float
    region: str
    intensity_g_per_kwh: float


class EnergyModelEntry(BaseModel):
    model: str
    calls: int
    tokens: int
    wh: float
    co2_grams: float


class EnergyAgentEntry(BaseModel):
    agent_id: str
    calls: int
    tokens: int
    wh: float
    co2_grams: float


class EnergySummaryResponse(BaseModel):
    """Body of ``GET /energy/summary``."""

    total_calls: int
    total_tokens: int
    total_wh: float
    total_co2_grams: float
    co2_kg: float
    equivalent_km_driven: float = Field(
        ..., description="grams CO2 / 120 (typical gasoline car)."
    )
    equivalent_phone_charges: float = Field(
        ..., description="total_wh / 10 (1 phone charge ≈ 10 Wh)."
    )
    equivalent_kettle_boils: float = Field(
        ..., description="total_wh / 100 (1.5L kettle boil ≈ 100 Wh)."
    )
    per_model: list[EnergyModelEntry]
    per_agent: list[EnergyAgentEntry]
    since: str | None = None
    until: str | None = None
    has_data: bool


# ───────────────────────── /consensus ─────────────────────────


class ConsensusEvaluateRequest(BaseModel):
    """Body of ``POST /consensus/evaluate``."""

    messages: list[dict[str, Any]] = Field(
        ..., description="OTel GenAI-style messages list."
    )
    models: list[str] = Field(
        ...,
        min_length=1,
        description="Models to query in parallel. Cost = N × single call.",
    )
    max_tokens: int = Field(
        500, ge=1, le=8192, description="Max tokens per model response."
    )
    threshold: float = Field(
        0.7,
        ge=0.0,
        le=1.0,
        description="Agreement threshold for consensus_reached.",
    )


class ConsensusModelResponse(BaseModel):
    """One model's reply within a consensus round."""

    model: str
    response: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float
    error: str | None = Field(
        None, description="Error string if the call failed; None on success."
    )


class ConsensusEvaluateResponse(BaseModel):
    """Body of ``POST /consensus/evaluate``."""

    models_queried: list[str]
    responses: list[ConsensusModelResponse]
    agreement_score: float = Field(
        ..., description="Pairwise-mean Jaccard token overlap in [0,1]."
    )
    consensus_reached: bool
    threshold: float
    disagreement_details: list[str] = Field(
        default_factory=list,
        description="Top-3 model-pairs by lowest pairwise agreement, "
        "populated when consensus_reached=False.",
    )
    recommended_response: str = Field(
        ..., description="Response from the highest-cost successful model."
    )
    recommended_model: str
    cost_total_usd: float
    latency_ms: float = Field(
        ..., description="Wall-clock duration of the parallel vote()."
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Per-model errors. Empty when all calls succeeded.",
    )


class ConsensusStakesRequest(BaseModel):
    """Body of ``POST /consensus/stakes``."""

    messages: list[dict[str, Any]] = Field(
        ..., description="Messages to classify."
    )


class ConsensusStakesResponse(BaseModel):
    """Body of ``POST /consensus/stakes``."""

    stakes: str = Field(..., description="'high' / 'low'.")
    keywords_found: list[str] = Field(
        default_factory=list,
        description="High-stakes keywords matched in the prompt text.",
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
        None, description="Export schema version, e.g. 'bijotel-chain-v1' or v2."
    )


# ─────────── /archive + /keygen + /verify-continuity (v2.3.0) ───────────


class KeygenResponse(BaseModel):
    """Body of ``POST /keygen``.

    Returns the PUBLIC key inline (caller distributes to auditors) and
    the on-disk path of the PRIVATE key (which never leaves the server).
    """

    private_key_path: str = Field(
        ..., description="Filesystem path of the PEM private key on the server."
    )
    public_key_pem: str = Field(
        ..., description="PEM-encoded public key, safe to distribute."
    )
    fingerprint: str = Field(
        ..., description="SHA-256(raw 32-byte pubkey)[:16] — short display id."
    )


class KeygenRequest(BaseModel):
    """Body of ``POST /keygen``."""

    output_dir: str = Field(
        "./keys",
        description="Server-side directory to write bijotel_private.pem + "
        "bijotel_public.pem into. Created if missing.",
    )
    force: bool = Field(
        False,
        description="Overwrite an existing private key. Don't pass true "
        "unless you really mean to rotate the signing key.",
    )


class ArchiveRequest(BaseModel):
    """Body of ``POST /archive``."""

    output_path: str = Field(
        ...,
        description="Destination SQLite path for the archive (must not exist).",
    )
    before_seq: int | None = Field(
        None, description="Archive rows with seq < this. Mutually exclusive with before_iso."
    )
    before_iso: str | None = Field(
        None,
        description="Archive rows with timestamp_ns before this UTC date "
        "(YYYY-MM-DD). Mutually exclusive with before_seq.",
    )
    sign_key_path: str | None = Field(
        None,
        description="Optional Ed25519 PEM private key path. When set, emit a "
        "signed JSON sidecar of the archive slice next to the SQLite file.",
    )
    dry_run: bool = Field(
        False,
        description="When true, report the plan and DO NOT write the archive "
        "or delete from source.",
    )


class ArchiveResponse(BaseModel):
    """Body of ``POST /archive``."""

    dry_run: bool
    archived_count: int
    first_seq: int
    last_seq: int
    first_prev_hash: str
    last_hmac_hash: str
    boundary_next_prev_hash: str | None
    archive_path: str
    segment_json_path: str | None = None
    main_remaining_count: int


class ContinuitySegmentSummary(BaseModel):
    """One segment in ``POST /verify-continuity`` response."""

    db_path: str
    valid: bool
    count: int | None = None
    first_seq: int | None = None
    last_seq: int | None = None
    first_prev_hash: str | None = None
    last_hmac_hash: str | None = None
    reason: str | None = None


class ContinuityBoundaryCheck(BaseModel):
    """One adjacent-pair boundary check."""

    from_db: str
    to_db: str
    matches: bool
    expected: str | None = None
    actual: str | None = None
    reason: str | None = None


class VerifyContinuityRequest(BaseModel):
    """Body of ``POST /verify-continuity``."""

    db_paths: list[str] = Field(
        ...,
        min_length=1,
        description="Two or more chain DB paths in chronological order "
        "(oldest archive first, live chain.db last). Single-segment input "
        "still works — it just verifies the one segment with no boundary "
        "checks.",
    )


class VerifyContinuityResponse(BaseModel):
    """Body of ``POST /verify-continuity``."""

    valid: bool
    segments: list[ContinuitySegmentSummary]
    boundaries: list[ContinuityBoundaryCheck]
