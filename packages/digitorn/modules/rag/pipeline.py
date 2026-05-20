"""RagPipeline - configurable retrieval orchestrator."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .backends.base import VectorBackend
from .bm25 import BM25Index
from .citations import Citation, RetrievalResult
from .config import PipelineConfig
from .embeddings import EmbeddingManager, ResolvedModel
from .fusion import FusionCandidate, reciprocal_rank_fusion
from .reranker import CrossEncoderReranker

if TYPE_CHECKING:
    from .cache import SemanticCache

logger = logging.getLogger(__name__)

class RagPipeline:
    """Executes the full retrieval pipeline for a single knowledge base query."""

    def __init__(
        self,
        backend: VectorBackend,
        embedding_mgr: EmbeddingManager,
        config: PipelineConfig,
        reranker: CrossEncoderReranker | None = None,
        cache: SemanticCache | None = None,
    ) -> None:
        self._backend = backend
        self._emb = embedding_mgr
        self._cfg = config
        self._reranker = reranker
        self._cache = cache

    async def retrieve(
        self,
        query: str,
        collection: str,
        model: ResolvedModel,
        bm25: BM25Index,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        strategy: str = "",
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        strat = strategy or self._cfg.retrieval
        rerank_n = self._cfg.rerank_top_n if self._reranker else 0
        fetch_k = max(top_k * 3, rerank_n) if rerank_n else top_k * 3

        if strat == "semantic":
            results = await self._semantic(
                query, collection, model, fetch_k, min_score, metadata_filter,
            )
        elif strat == "bm25":
            results = await self._bm25_with_text(
                query, collection, bm25, fetch_k,
            )
        else:
            results = await self._hybrid(
                query, collection, model, bm25, fetch_k, min_score, metadata_filter,
            )

        if self._reranker and rerank_n and results:
            results = self._apply_rerank(query, results, rerank_n)

        return results[:top_k]

    async def retrieve_streaming(
        self,
        query: str,
        collection: str,
        model: ResolvedModel,
        bm25: BM25Index,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        metadata_filter: dict[str, Any] | None = None,
        timeout: float = 0.3,
    ) -> tuple[list[RetrievalResult], list[RetrievalResult]]:
        """Streaming retrieval - returns (fast_results, late_results)."""
        sem_task = asyncio.create_task(self._semantic(
            query, collection, model, top_k * 3, min_score, metadata_filter,
        ))
        bm25_task = asyncio.create_task(self._bm25_with_text(
            query, collection, bm25, top_k * 3,
        ))

        done, pending = await asyncio.wait(
            [sem_task, bm25_task],
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        fast: list[RetrievalResult] = []
        for task in done:
            try:
                fast.extend(task.result())
            except Exception as exc:
                logger.debug("Streaming retrieval task failed: %s", exc)

        fast = self._deduplicate(fast)
        if self._reranker and fast:
            fast = self._apply_rerank(query, fast, min(len(fast), 10))
        fast = fast[:top_k]

        late: list[RetrievalResult] = []
        if pending:
            late_done, _ = await asyncio.wait(pending, timeout=5.0)
            for task in late_done:
                try:
                    late.extend(task.result())
                except Exception as exc:
                    logger.debug("pipeline best-effort block failed: %s", exc)
            late = [r for r in late if r.doc_id not in {f.doc_id for f in fast}]

        return fast, late

    async def _semantic(
        self,
        query: str,
        collection: str,
        model: ResolvedModel,
        top_k: int,
        min_score: float,
        filt: dict[str, Any] | None,
    ) -> list[RetrievalResult]:
        # Off-loop: CPU-bound, can stall the loop on first model load.
        import asyncio as _asyncio
        vec = await _asyncio.to_thread(self._emb.embed_single, query, model)
        raw = await self._backend.search(
            collection, vec, top_k=top_k, min_score=min_score, filter=filt,
        )
        return [
            RetrievalResult(
                text=r.text,
                score=r.score,
                doc_id=r.doc_id,
                citation=_citation_from_meta(r.metadata, r.score),
            )
            for r in raw
        ]

    async def _bm25_with_text(
        self,
        query: str,
        collection: str,
        bm25: BM25Index,
        top_k: int,
    ) -> list[RetrievalResult]:
        hits = bm25.search(query, top_k=top_k)
        if not hits:
            return []

        doc_ids = [doc_id for doc_id, _ in hits]
        score_map = dict(hits)
        docs = await self._backend.get(collection, doc_ids)
        doc_map = {d.doc_id: d for d in docs}

        results = []
        for doc_id in doc_ids:
            d = doc_map.get(doc_id)
            if d is None:
                continue
            results.append(RetrievalResult(
                text=d.text,
                score=score_map[doc_id],
                doc_id=doc_id,
                citation=_citation_from_meta(d.metadata, score_map[doc_id]),
            ))
        return results

    async def _hybrid(
        self,
        query: str,
        collection: str,
        model: ResolvedModel,
        bm25: BM25Index,
        top_k: int,
        min_score: float,
        filt: dict[str, Any] | None,
    ) -> list[RetrievalResult]:
        sem_results = await self._semantic(
            query, collection, model, top_k, min_score, filt,
        )
        bm25_hits = bm25.search(query, top_k=top_k)

        sem_candidates = [
            FusionCandidate(r.doc_id, r.score, {"result": r}, "semantic")
            for r in sem_results
        ]
        bm25_candidates = [
            FusionCandidate(doc_id, score, {}, "bm25")
            for doc_id, score in bm25_hits
        ]

        fused = reciprocal_rank_fusion([sem_candidates, bm25_candidates], top_k=top_k)

        sem_lookup = {r.doc_id: r for r in sem_results}
        results: list[RetrievalResult] = []

        for fc in fused:
            if fc.doc_id in sem_lookup:
                r = sem_lookup[fc.doc_id]
                results.append(RetrievalResult(
                    text=r.text, score=fc.score, doc_id=fc.doc_id, citation=r.citation,
                ))
            else:
                docs = await self._backend.get(collection, [fc.doc_id])
                if docs:
                    d = docs[0]
                    results.append(RetrievalResult(
                        text=d.text,
                        score=fc.score,
                        doc_id=fc.doc_id,
                        citation=_citation_from_meta(d.metadata, fc.score),
                    ))

        return results

    def _apply_rerank(
        self,
        query: str,
        results: list[RetrievalResult],
        top_n: int,
    ) -> list[RetrievalResult]:
        texts = [r.text for r in results[:top_n]]
        if not texts:
            return results

        try:
            ranked = self._reranker.rerank(query, texts, top_k=top_n)  # type: ignore[union-attr]
        except Exception:
            logger.warning("Reranker failed, returning unranked results", exc_info=True)
            return results

        reranked = []
        for idx, score in ranked:
            if idx < len(results):
                r = results[idx]
                reranked.append(RetrievalResult(
                    text=r.text,
                    score=score,
                    doc_id=r.doc_id,
                    citation=Citation(
                        source_type=r.citation.source_type,
                        source_id=r.citation.source_id,
                        location=r.citation.location,
                        content_hash=r.citation.content_hash,
                        timestamp=r.citation.timestamp,
                        confidence=score,
                        metadata=r.citation.metadata,
                    ),
                ))
        return reranked

    @staticmethod
    def _deduplicate(results: list[RetrievalResult]) -> list[RetrievalResult]:
        seen: set[str] = set()
        deduped: list[RetrievalResult] = []
        for r in results:
            if r.doc_id not in seen:
                seen.add(r.doc_id)
                deduped.append(r)
        return deduped

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
