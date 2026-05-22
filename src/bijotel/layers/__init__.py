"""BIJOTEL layers: pluggable specialty processors beyond core hardening.

Each layer is independently opt-in via optional dependency extras:

  pip install bijotel[fingerprint]   # F13 / Bijuteria #7: semantic fingerprinting

Layers compose with the core SpanProcessor stack (HmacChain + CAS) — they
are SpanProcessors themselves and follow the same on_end contract,
including crash isolation and WAL+IMMEDIATE persistence semantics.

Layers harvested from upstream Aisophical projects preserve attribution
in the module-level docstring (provenance + MIT license inheritance).
"""

from bijotel.layers.fingerprint import (
    DeterministicFingerprinter,
    FingerprintSpanProcessor,
    SemanticFingerprinter,
    similarity_search,
)

__all__ = [
    "DeterministicFingerprinter",
    "FingerprintSpanProcessor",
    "SemanticFingerprinter",
    "similarity_search",
]
