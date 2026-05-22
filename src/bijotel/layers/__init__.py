"""BIJOTEL layers: pluggable specialty processors beyond core hardening.

Each layer is independently opt-in via optional dependency extras:

  pip install bijotel[fingerprint]   # F13 / Bijuteria #7: semantic fingerprinting

Layers compose with the core SpanProcessor stack (HmacChain + CAS) — they
are SpanProcessors themselves and follow the same on_end contract,
including crash isolation and WAL+IMMEDIATE persistence semantics.

Layers harvested from upstream Aisophical projects preserve attribution
in the module-level docstring (provenance + MIT license inheritance).
"""

from bijotel.layers.ast_safety import (
    ASTSafetyChecker,
    ASTViolation,
    ast_safety_check,
)
from bijotel.layers.containment import (
    ContainmentDecision,
    ContainmentGuard,
)
from bijotel.layers.fingerprint import (
    DeterministicFingerprinter,
    FingerprintSpanProcessor,
    SemanticFingerprinter,
    similarity_search,
)
from bijotel.layers.misalignment import (
    CategoryResult,
    MisalignmentReport,
    Probe,
    ProbeLibrary,
    ProbeResult,
    misalignment_check,
)
from bijotel.layers.routing import (
    Budget,
    ModelProfile,
    ModelRegistry,
    ParetoRouter,
    RoutingAdvice,
    TaskClassifier,
    routing_recommendation,
)

__all__ = [
    "ASTSafetyChecker",
    "ASTViolation",
    "Budget",
    "CategoryResult",
    "ContainmentDecision",
    "ContainmentGuard",
    "DeterministicFingerprinter",
    "FingerprintSpanProcessor",
    "MisalignmentReport",
    "ModelProfile",
    "ModelRegistry",
    "ParetoRouter",
    "Probe",
    "ProbeLibrary",
    "ProbeResult",
    "RoutingAdvice",
    "SemanticFingerprinter",
    "TaskClassifier",
    "ast_safety_check",
    "misalignment_check",
    "routing_recommendation",
    "similarity_search",
]
