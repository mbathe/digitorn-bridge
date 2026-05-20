"""HybridStrategy - BM25 + semantic search fused via RRF."""

from __future__ import annotations

import logging
from typing import Any

from ..backends.base import VectorBackend
from ..bm25 import BM25Index
from ..citations import Citation, RetrievalResult
from ..config import PipelineConfig
from ..embeddings import EmbeddingManager, ResolvedModel
from ..fusion import FusionCandidate, reciprocal_rank_fusion
from .base import RetrievalStrategy

logger = logging.getLogger(__name__)

class HybridStrategy(RetrievalStrategy):

    def __init__(
        self,
        backend: VectorBackend,
        embedding_mgr: EmbeddingManager,
        model: ResolvedModel,
        bm25: BM25Index,
        collection: str,
        config: PipelineConfig,
    ) -> None:
        self._backend = backend
        self._emb = embedding_mgr
        self._model = model
        self._bm25 = bm25
        self._collection = collection
        self._cfg = config

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[RetrievalResult]:
        fetch_k = top_k * 3

        # Off-loop: embedding is CPU-bound (fastembed) and on first
        # query a model load can take seconds.
        import asyncio as _asyncio
        vec = await _asyncio.to_thread(self._emb.embed_single, query, self._model)
        sem_raw = await self._backend.search(
            self._collection, vec, top_k=fetch_k,
            min_score=min_score, filter=metadata_filter,
        )

        sem_results = [
            RetrievalResult(
                text=r.text, score=r.score, doc_id=r.doc_id,
                citation=_citation_from_meta(r.metadata, r.score),
            )
            for r in sem_raw
        ]

        bm25_hits = self._bm25.search(query, top_k=fetch_k)

        sem_candidates = [
            FusionCandidate(r.doc_id, r.score, {"result": r}, "semantic")
            for r in sem_results
        ]
        bm25_candidates = [
            FusionCandidate(doc_id, score, {}, "bm25")
            for doc_id, score in bm25_hits
        ]

        fused = reciprocal_rank_fusion(
            [sem_candidates, bm25_candidates], top_k=top_k,
        )

        sem_lookup = {r.doc_id: r for r in sem_results}
        results: list[RetrievalResult] = []

        for fc in fused:
            if fc.doc_id in sem_lookup:
                r = sem_lookup[fc.doc_id]
                results.append(RetrievalResult(
                    text=r.text, score=fc.score, doc_id=fc.doc_id,
                    citation=r.citation,
                ))
            else:
                docs = await self._backend.get(self._collection, [fc.doc_id])
                if docs:
                    d = docs[0]
                    results.append(RetrievalResult(
                        text=d.text, score=fc.score, doc_id=fc.doc_id,
                        citation=_citation_from_meta(d.metadata, fc.score),
                    ))

        return results

def _citation_from_meta(meta: dict[str, Any], score: float) -> Citation:
    parts = []
    if "chunk_index" in meta:
        parts.append(f"chunk {meta['chunk_index']}")
    if "page" in meta:
        parts.append(f"page {meta['page']}")
    if "section" in meta:
        parts.append(f"section: {meta['section']}")
    if "start_char" in meta and "end_char" in meta:
        parts.append(f"chars {meta['start_char']}-{meta['end_char']}")

    return Citation(
        source_type=meta.get("source_type", "manual"),
        source_id=meta.get("source_id", "unknown"),
        location=", ".join(parts),
        confidence=score,
    )
