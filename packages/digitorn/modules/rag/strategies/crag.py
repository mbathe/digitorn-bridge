"""CRAGStrategy — Corrective RAG with quality evaluation and fallback.

Evaluates retrieved documents for relevance. If quality is below threshold,
triggers a broader query or alternative retrieval path.
"""

from __future__ import annotations

import logging
from typing import Any

from ..citations import RetrievalResult
from .base import RetrievalStrategy

logger = logging.getLogger(__name__)

_EVAL_PROMPT = (
    "Evaluate if the following retrieved document is relevant to the query.\n\n"
    "Query: {query}\n\n"
    "Document:\n{document}\n\n"
    "Rate relevance from 0.0 to 1.0. Return ONLY the number."
)


class CRAGStrategy(RetrievalStrategy):

    def __init__(
        self,
        inner: RetrievalStrategy,
        *,
        llm_caller: Any = None,
        confidence_threshold: float = 0.5,
        fallback: str = "broader_query",
    ) -> None:
        self._inner = inner
        self._llm = llm_caller
        self._threshold = confidence_threshold
        self._fallback = fallback

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[RetrievalResult]:
        results = await self._inner.retrieve(
            query, top_k=top_k, min_score=min_score,
            metadata_filter=metadata_filter, **kwargs,
        )

        if not results:
            return await self._try_fallback(query, top_k, min_score, metadata_filter, **kwargs)

        if self._llm is None:
            return self._filter_by_score(results)

        evaluated = await self._evaluate_results(query, results)

        if not evaluated:
            return await self._try_fallback(query, top_k, min_score, metadata_filter, **kwargs)

        return evaluated

    async def _evaluate_results(
        self, query: str, results: list[RetrievalResult],
    ) -> list[RetrievalResult]:
        kept: list[RetrievalResult] = []

        for r in results[:8]:
            try:
                prompt = _EVAL_PROMPT.format(query=query, document=r.text[:1000])
                raw = await self._llm(prompt)
                score = float(str(raw).strip())
                if score >= self._threshold:
                    kept.append(r)
            except (ValueError, TypeError):
                if r.score >= self._threshold:
                    kept.append(r)
            except Exception as exc:
                logger.debug("CRAG eval failed: %s", exc)
                kept.append(r)

        return kept

    def _filter_by_score(self, results: list[RetrievalResult]) -> list[RetrievalResult]:
        return [r for r in results if r.score >= self._threshold]

    async def _try_fallback(
        self,
        query: str,
        top_k: int,
        min_score: float,
        metadata_filter: dict[str, Any] | None,
        **kwargs: Any,
    ) -> list[RetrievalResult]:
        if self._fallback == "broader_query":
            words = query.split()
            if len(words) > 3:
                broader = " ".join(words[:len(words) // 2 + 1])
                return await self._inner.retrieve(
                    broader, top_k=top_k, min_score=min_score * 0.5,
                    metadata_filter=metadata_filter, **kwargs,
                )
        return []
