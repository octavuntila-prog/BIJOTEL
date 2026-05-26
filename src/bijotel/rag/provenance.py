"""RAG source provenance — capture which retrieved chunks informed an LLM call.

The goal is small and concrete: when your app calls an LLM with retrieved
context, attach a JSON-serializable record of where that context came from
to the span. BIJOTEL then seals that record into the HMAC chain as part of
the canonical body, so it becomes tamper-evident along with the rest of
the call.

This addresses the "decision based on what input" gap in regulatory
frameworks such as the EU AI Act Article 12 (record-keeping for high-risk
AI systems) and ISO/IEC 42001 §9 (operational data lineage).

Attribute namespace (all under ``bijotel.rag.*``):

    bijotel.rag.source_count          # int
    bijotel.rag.sources               # JSON-encoded list[RAGSource as dict]
    bijotel.rag.retriever_id          # str — primary retriever, or ""
    bijotel.rag.embedding_model       # str — primary embedding model, or ""
    bijotel.rag.total_context_tokens  # int — optional, present iff caller sets it

Usage:

    >>> from bijotel.rag import RAGSource, rag_context
    >>> sources = [
    ...     RAGSource(
    ...         document_id="sha256:abc123...",
    ...         chunk_index=0,
    ...         source_uri="s3://corpus/eu-ai-act.pdf",
    ...         retriever="qdrant",
    ...         embedding_model="text-embedding-3-small",
    ...         similarity_score=0.89,
    ...         retrieved_at="2026-05-26T10:00:00Z",
    ...     ),
    ... ]
    >>> attrs = rag_context(sources)
    >>> # Attach to your span before the LLM call:
    >>> for k, v in attrs.items():
    ...     span.set_attribute(k, v)

Or use the decorator form on a function that wraps the LLM call:

    >>> @with_rag_provenance(sources)
    ... def ask_with_context(query: str) -> str:
    ...     return client.messages.create(...)

Both forms write to the *current* OpenTelemetry span — the caller is
responsible for ensuring one is active (typically inside a
``tracer.start_as_current_span(...)`` block).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import wraps
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class RAGSource:
    """Descriptor for a single retrieved chunk that informed an LLM call.

    All fields are intentionally JSON-serializable scalars/strings so the
    record canonicalizes cleanly (RFC 8785) into the chain body.

    Attributes:
        document_id: Stable identifier for the source document.
            Convention: ``sha256:<hex>`` of the original bytes. Any prefix
            scheme works (``s3://``, ``urn:...``); pick one and stick with
            it across your fleet so chains compare apples to apples.
        chunk_index: Zero-based index of the chunk inside the document.
        source_uri: Where the document lives (S3 URI, file path, URL).
            Optional — empty string is acceptable when only the
            ``document_id`` is meaningful (e.g. pure content-hash routing).
        retriever: Retriever name (``qdrant``, ``pinecone``, ``elasticsearch``,
            ``bm25``, ...). One token, no version suffix.
        embedding_model: Embedding model that produced the vectors. Use the
            same identifier you would in OTel ``gen_ai.request.model``
            (e.g. ``text-embedding-3-small``).
        similarity_score: Retrieval similarity in ``[0.0, 1.0]``. Bigger =
            more relevant. Cosine similarity is the typical interpretation
            but any monotonic score works; the chain stores it verbatim.
        retrieved_at: ISO-8601 timestamp (UTC, ``Z`` suffix) of the retrieval.
            Optional; empty string means "unknown / not recorded".
        metadata: Free-form per-source metadata. Stays JSON-serializable.
            Use this for retriever-specific fields (e.g. Qdrant payload
            keys) that don't fit the schema above. Default empty dict.
    """

    document_id: str
    chunk_index: int
    source_uri: str = ""
    retriever: str = ""
    embedding_model: str = ""
    similarity_score: float = 0.0
    retrieved_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable dict shape used inside the chain."""
        return asdict(self)


def rag_context(
    sources: list[RAGSource],
    *,
    total_context_tokens: int | None = None,
) -> dict[str, Any]:
    """Build the ``bijotel.rag.*`` attribute dict for an upcoming LLM call.

    The returned dict is suitable for passing to ``span.set_attribute`` in
    a loop, or for merging into a span's attribute kwargs. All values are
    OTel-attribute-compatible (str / int / float / bool — sources is
    JSON-encoded to fit the string slot).

    Args:
        sources: Retrieved chunks that informed the call. May be empty —
            the result still includes ``bijotel.rag.source_count = 0`` so
            "tried RAG, got nothing" is distinguishable from "did not use
            RAG at all" (which omits the attribute entirely).
        total_context_tokens: Optional total token count of the retrieved
            context as it was concatenated into the prompt. Set when you
            can measure it (most retrievers can); leave ``None`` when you
            cannot, and the attribute is simply omitted.

    Returns:
        Dict with the four required keys (``source_count``, ``sources``,
        ``retriever_id``, ``embedding_model``) plus ``total_context_tokens``
        if provided. The ``sources`` value is a JSON string (not a Python
        list) — OTel attributes must be primitives.

    Note:
        The "primary" retriever and embedding model are taken from the
        *first* source. Real-world RAG pipelines almost always use a single
        retriever + embedding model per call, so this is correct in
        practice. If you need to record a hybrid pipeline, encode that in
        the per-source ``metadata`` field; the chain stores everything.
    """
    primary_retriever = sources[0].retriever if sources else ""
    primary_embedding = sources[0].embedding_model if sources else ""
    attrs: dict[str, Any] = {
        "bijotel.rag.source_count": len(sources),
        "bijotel.rag.sources": json.dumps(
            [s.to_dict() for s in sources], sort_keys=True
        ),
        "bijotel.rag.retriever_id": primary_retriever,
        "bijotel.rag.embedding_model": primary_embedding,
    }
    if total_context_tokens is not None:
        attrs["bijotel.rag.total_context_tokens"] = int(total_context_tokens)
    return attrs


def with_rag_provenance(
    sources: list[RAGSource],
    *,
    total_context_tokens: int | None = None,
) -> Callable[[F], F]:
    """Decorator: attach RAG provenance to the currently active span.

    Applies ``rag_context(sources, ...)`` to the span returned by
    ``trace.get_current_span()`` *before* the wrapped function runs. If no
    span is active (or the no-op span is returned), the decorator is a
    silent pass-through — provenance only sticks when there is a real span
    to attach it to.

    Args:
        sources: Same shape as for ``rag_context``.
        total_context_tokens: Forwarded to ``rag_context``.

    Returns:
        A decorator that wraps the target callable.

    Example:
        >>> @with_rag_provenance(retrieved_chunks)
        ... def answer(query: str) -> str:
        ...     return llm.complete(query)

    The decorator is intentionally synchronous — async callers should
    instead call ``span.set_attribute`` from ``rag_context`` directly
    inside their async function, where the active-span context is
    correctly propagated by OTel's contextvars.
    """
    attrs = rag_context(sources, total_context_tokens=total_context_tokens)

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Local import: keep the OTel dependency out of import time
            # of bijotel.rag for users who only want the dataclass.
            from opentelemetry import trace

            span = trace.get_current_span()
            # An inactive span returns the no-op INVALID context — its
            # set_attribute is a harmless no-op, so we don't gate on
            # is_recording() here. Cheap and correct.
            for key, value in attrs.items():
                span.set_attribute(key, value)
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
