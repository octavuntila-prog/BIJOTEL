"""``bijotel.rag`` — RAG source provenance for LLM audit chains (v2.6.0).

When an application uses retrieval-augmented generation (RAG), the chain
entry should record *which documents informed the answer*, not only the
final prompt/response. This module provides the small surface that lets a
caller attach that evidence to an OpenTelemetry span before the LLM call.

The attributes land in ``span.attributes`` under the ``bijotel.rag.*``
namespace and are picked up by ``span_to_canonical_dict`` automatically
(every attribute is included in the canonical body — see
``processors/canonical.py``). Once the span ends, the HMAC chain seals the
sources alongside everything else, so an auditor can later prove which
documents the model saw.

Public API:
    ``RAGSource``                — dataclass describing one retrieved chunk.
    ``rag_context(sources)``     — builds the ``bijotel.rag.*`` attribute dict.
    ``with_rag_provenance(...)`` — decorator that attaches the attrs to the
                                   currently active span.
"""

from __future__ import annotations

from bijotel.rag.provenance import (
    RAGSource,
    rag_context,
    with_rag_provenance,
)

__all__ = [
    "RAGSource",
    "rag_context",
    "with_rag_provenance",
]
