# RAG Source Provenance

**Available from v2.6.0.**

When your application uses retrieval-augmented generation, the BIJOTEL
chain entry should record *which documents informed the answer*, not
only the final prompt and response. This page explains the surface that
captures that evidence.

## Why this matters

Regulatory frameworks for high-risk AI systems treat retrieved context
as part of the input that drove a decision:

- **EU AI Act Article 12** requires records sufficient to reconstruct
  the decision a high-risk system made — which includes the data the
  model saw, not just the final tokens.
- **ISO/IEC 42001 §9** asks for operational data lineage end-to-end.
- **NIST AI RMF GV-1.3** describes provenance as a control over input
  integrity.

Without RAG provenance, an audit can only answer "what did the model
say?" — not "what did it know when it said it?" That gap is where most
real-world LLM incidents live.

## What gets captured

Five span attributes under the `bijotel.rag.*` namespace:

| Attribute                            | Type   | Purpose                                          |
|--------------------------------------|--------|--------------------------------------------------|
| `bijotel.rag.source_count`           | int    | Number of retrieved chunks that fed the call     |
| `bijotel.rag.sources`                | JSON   | Per-chunk descriptors (see below)                |
| `bijotel.rag.retriever_id`           | str    | Primary retriever name (qdrant, pinecone, ...)   |
| `bijotel.rag.embedding_model`        | str    | Primary embedding model identifier               |
| `bijotel.rag.total_context_tokens`   | int    | Optional — tokens of retrieved context in prompt |

Each entry in `bijotel.rag.sources` is the JSON form of a `RAGSource`:

```json
{
  "document_id": "sha256:abc123...",
  "chunk_index": 0,
  "source_uri": "s3://corpus/eu-ai-act.pdf",
  "retriever": "qdrant",
  "embedding_model": "text-embedding-3-small",
  "similarity_score": 0.89,
  "retrieved_at": "2026-05-26T10:00:00Z",
  "metadata": {}
}
```

These attributes flow into the canonical body automatically — there is
no separate sealing step. Once the span ends, the HMAC chain hashes them
together with everything else, so tampering with the source list
invalidates the chain.

## Wiring it into your RAG pipeline

Three lines, between retrieval and the LLM call.

### Manual form

```python
from bijotel.rag import RAGSource, rag_context

# After your retriever returns hits:
sources = [
    RAGSource(
        document_id=f"sha256:{hit.doc_hash}",
        chunk_index=hit.chunk_idx,
        source_uri=hit.uri,
        retriever="qdrant",
        embedding_model="text-embedding-3-small",
        similarity_score=hit.score,
        retrieved_at=now_iso(),
    )
    for hit in retriever_hits
]

# Inside an active span (your existing one from start_as_current_span):
for key, value in rag_context(sources, total_context_tokens=ctx_tokens).items():
    span.set_attribute(key, value)

# Then call the LLM as usual.
response = client.messages.create(...)
```

### Decorator form

For functions whose only job is to call the LLM with retrieved context:

```python
from bijotel.rag import with_rag_provenance

@with_rag_provenance(sources, total_context_tokens=ctx_tokens)
def answer(query: str) -> str:
    return client.messages.create(
        model="claude-haiku-4-5",
        messages=[{"role": "user", "content": query}],
    ).content[0].text
```

The decorator attaches the attributes to the currently active span
before running the wrapped function. There must be an open span around
the call site — `trace.get_current_span()` is what the decorator reads.

### Empty retrieval is still recorded

Pass `sources=[]` and you get `bijotel.rag.source_count = 0`. That is
deliberately *different* from a non-RAG call (which has no
`bijotel.rag.*` attributes at all). The distinction matters for audits:
"tried RAG and got zero hits" is a different incident class from "did
not use RAG."

## Viewing the sources

`bijotel inspect` surfaces RAG provenance whenever the chain entry
carries it:

```
$ bijotel inspect --db chain.db 6373
=== Span Metadata ===
seq:                 6373
...

=== Status: ALLOWED ===
Cost (real): $0.0023

=== RAG Provenance ===
source_count:   3
retriever:      qdrant
embedding:      text-embedding-3-small
context_tokens: 720
  source 1: doc=sha256:abc1...  chunk=0  retriever=qdrant  sim=0.890
  source 2: doc=sha256:def4...  chunk=3  retriever=qdrant  sim=0.760
  source 3: doc=sha256:ee52...  chunk=1  retriever=qdrant  sim=0.713
```

Above ~5 sources, only the first 5 print; the rest collapse into
`... and N more`. The full set remains in the canonical-body dump that
follows.

## Regression on retrieval

The F12 regression detector knows a new dimension in v2.6.0:
`rag_source_count`. Drift in retrieved-chunk counts is a real
operational signal:

- A retriever index that has silently shrunk (replicas lost) returns
  fewer hits per call.
- A new filter accidentally added to the query path narrows results.
- A model's context window changes and the pipeline trims more
  aggressively.

Run it like any other dimension:

```
$ bijotel regression --db chain.db --dimension rag_source_count
```

Non-RAG entries return `None` for this dimension, so the detector
silently ignores them. Mixed chains (some RAG, some not) work fine.

## Interaction with semantic dedup (CAS)

The chain stores `bijotel.rag.sources` as a JSON blob that includes
per-call data (timestamps, similarity scores). Two retrievals against
the same corpus rarely produce byte-identical JSON, so we exclude that
single field from the semantic-dedup key.

The *stable* RAG fields stay in the dedup key:

| Field                            | In dedup? |
|----------------------------------|-----------|
| `bijotel.rag.source_count`       | yes       |
| `bijotel.rag.retriever_id`       | yes       |
| `bijotel.rag.embedding_model`    | yes       |
| `bijotel.rag.total_context_tokens` | yes     |
| `bijotel.rag.sources`            | **no**    |

This means identical-prompt + identical-retriever calls still match on
dedup, even when the per-call timestamps differ. Switching retrievers
or embedding models breaks the match — which is correct: a different
retriever is a different call.

## Public API

The full surface lives under `bijotel.rag`:

```python
from bijotel.rag import (
    RAGSource,             # frozen dataclass
    rag_context,           # build attribute dict
    with_rag_provenance,   # decorator form
)
```

Each is also re-exported from `bijotel` directly for the short form
`from bijotel import RAGSource`.

## Honest caveat

BIJOTEL stores hashes (`document_id`) and identifiers (`source_uri`),
not the document bytes themselves. To prove later that the document the
chain references is the same as what you serve today, you need a
separate corpus-versioning system that can resolve
`sha256:abc123...` back to the original bytes — typically your object
store with content-addressed paths, or a Git-LFS / IPFS layer.

The chain proves "this call referenced document X at time T". Whether
your storage still has document X bit-for-bit at audit time is a
question of how you store, not how you log.
