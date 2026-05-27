"""BIJOTEL: SpanProcessor plug-ins for OpenTelemetry GenAI."""

from bijotel.adapters import (
    AnthropicAdapter,
    OpenAIAdapter,
    Provider,
    ProviderResponse,
)
from bijotel.anchoring import (
    REKOR_PUBLIC_URL,
    AnchorVerifyResult,
    RekorAnchor,
    anchor_chain_head,
    verify_rekor_anchor,
)
from bijotel.attestation import (
    AttestationQuote,
    SoftwareAttestation,
)
from bijotel.core.init import init, shutdown
from bijotel.crypto.ed25519 import (
    generate_keypair,
    load_private_pem,
    load_public_pem,
    public_key_fingerprint,
    public_key_raw_b64,
)
from bijotel.crypto.ed25519 import sign as ed25519_sign
from bijotel.crypto.ed25519 import verify as ed25519_verify
from bijotel.decorators import trace_genai, wrap
from bijotel.federation import (
    CrossAnchorReceipt,
    FederationClient,
    RegistrationReceipt,
    SubmissionReceipt,
    verify_cross_anchor_receipt,
)
from bijotel.integrity import (
    ChainIntegrityMonitor,
    IntegrityReport,
    analyze_chain_integrity,
)
from bijotel.cross_view import (
    ChainStats,
    CrossEcosystemView,
)
from bijotel.mcp import (
    MCPInstrumentor,
    mcp_invocation_context,
)
from bijotel.layers import (
    ASTSafetyChecker,
    ASTViolation,
    Budget,
    CarbonCalculator,
    ConsensusResult,
    ConsensusVoter,
    ContainmentDecision,
    ContainmentGuard,
    DeterministicFingerprinter,
    EnergyEstimator,
    EnergySpanProcessor,
    EnergySummary,
    EnergyTracker,
    FingerprintSpanProcessor,
    MisalignmentReport,
    ModelRegistry,
    ModelResponse,
    ParetoRouter,
    Probe,
    ProbeLibrary,
    SemanticFingerprinter,
    StakesClassifier,
    TaskClassifier,
    ast_safety_check,
    compute_agreement,
    consensus_requirement,
    energy_budget,
    misalignment_check,
    routing_recommendation,
    similarity_search,
)
from bijotel.policy import (
    Decision,
    PolicyDeniedError,
    PolicyEngine,
    cost_per_call_max,
    daily_token_budget,
    guard,
    model_allowlist,
    model_version_pin,
    output_length_limit,
    pii_detection,
    prompt_pattern_deny,
    rate_limit_calls_per_minute,
)
from bijotel.processors import (
    DAGNode,
    MerkleDAG,
    archive_chain,
    chain_range_summary,
    export_chain,
    inspect_export,
    verify_chain,
    verify_continuity,
    verify_export,
)
from bijotel.rag import (
    RAGSource,
    rag_context,
    with_rag_provenance,
)
from bijotel.regression import (
    Anomaly,
    AnomalyMethod,
    DimensionStats,
    RegressionDetector,
    compute_baseline,
)
from bijotel.replay import (
    ReplayResult,
    record_replay_context,
    verify_replay,
)

__version__ = "2.13.0"

__all__ = [
    "ASTSafetyChecker",
    "ASTViolation",
    "Anomaly",
    "AnomalyMethod",
    "AnchorVerifyResult",
    "AnthropicAdapter",
    "AttestationQuote",
    "Budget",
    "CarbonCalculator",
    "ChainIntegrityMonitor",
    "ChainStats",
    "ConsensusResult",
    "ConsensusVoter",
    "ContainmentDecision",
    "ContainmentGuard",
    "CrossAnchorReceipt",
    "CrossEcosystemView",
    "DAGNode",
    "Decision",
    "DeterministicFingerprinter",
    "DimensionStats",
    "EnergyEstimator",
    "EnergySpanProcessor",
    "EnergySummary",
    "EnergyTracker",
    "FederationClient",
    "FingerprintSpanProcessor",
    "IntegrityReport",
    "MCPInstrumentor",
    "MerkleDAG",
    "MisalignmentReport",
    "ModelRegistry",
    "ModelResponse",
    "OpenAIAdapter",
    "ParetoRouter",
    "PolicyDeniedError",
    "PolicyEngine",
    "Probe",
    "ProbeLibrary",
    "Provider",
    "ProviderResponse",
    "RAGSource",
    "REKOR_PUBLIC_URL",
    "RegistrationReceipt",
    "RegressionDetector",
    "RekorAnchor",
    "ReplayResult",
    "SemanticFingerprinter",
    "SoftwareAttestation",
    "StakesClassifier",
    "SubmissionReceipt",
    "TaskClassifier",
    "__version__",
    # v2.8.0 chain integrity public API
    "analyze_chain_integrity",
    # v2.9.0 Rekor anchoring public API
    "anchor_chain_head",
    # v2.2.0 chain segmentation + archival public API
    "archive_chain",
    "ast_safety_check",
    "chain_range_summary",
    "compute_agreement",
    "compute_baseline",
    "consensus_requirement",
    "cost_per_call_max",
    "daily_token_budget",
    # v2.1.0 Ed25519 asymmetric signature surface
    "ed25519_sign",
    "ed25519_verify",
    "energy_budget",
    "export_chain",
    "generate_keypair",
    "guard",
    "init",
    "inspect_export",
    "load_private_pem",
    "load_public_pem",
    # v2.12.0 MCP invocation sealing
    "mcp_invocation_context",
    "misalignment_check",
    "model_allowlist",
    "model_version_pin",
    "output_length_limit",
    "pii_detection",
    "prompt_pattern_deny",
    "public_key_fingerprint",
    "public_key_raw_b64",
    # v2.6.0 RAG provenance surface
    "rag_context",
    "rate_limit_calls_per_minute",
    # v2.7.0 replay verification surface
    "record_replay_context",
    "routing_recommendation",
    "shutdown",
    "similarity_search",
    "trace_genai",
    "verify_chain",
    "verify_continuity",
    "verify_cross_anchor_receipt",
    "verify_export",
    "verify_rekor_anchor",
    "verify_replay",
    "with_rag_provenance",
    "wrap",
]
