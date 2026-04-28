"""MultiQueryStrategy - LLM-generated query variants for broader recall.

Generates N variant phrasings of the original query, retrieves for each,
then fuses results via RRF to surface documents that a single query would miss.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..citations import RetrievalResult
from ..config import MultiQueryConfig
from ..fusion import FusionCandidate, reciprocal_rank_fusion
from .base import RetrievalStrategy

logger = logging.getLogger(__name__)

_VARIANT_PROMPT = (
    "Generate {n} alternative phrasings for this search query. "
    "Each variant should capture a different angle or use different keywords, "
    "while preserving the original intent.\n\n"
    "Original query: {query}\n\n"
    "Return ONLY the variants, one per line, no numbering."
)


class MultiQueryStrategy(RetrievalStrategy):

    def __init__(
        self,
        inner: RetrievalStrategy,
        config: MultiQueryConfig,
        llm_caller: Any = None,
    ) -> None:
        self._inner = inner
        self._cfg = config
        self._llm = llm_caller

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[RetrievalResult]:
        variants = await self._generate_variants(query)
        all_queries = [query] + variants

        tasks = [
            self._inner.retrieve(
                q, top_k=top_k, min_score=min_score,
                metadata_filter=metadata_filter, **kwargs,
            )
            for q in all_queries
        ]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        candidate_lists: list[list[FusionCandidate]] = []
        result_lookup: dict[str, RetrievalResult] = {}

        for result_set in all_results:
            if isinstance(result_set, Exception):
                logger.warning("Multi-query variant failed: %s", result_set)
                continue

            candidates = []
            for r in result_set:
                candidates.append(
                    FusionCandidate(r.doc_id, r.score, {}, "multiquery"),
                )
                if r.doc_id not in result_lookup:
                    result_lookup[r.doc_id] = r

            candidate_lists.append(candidates)

        if not candidate_lists:
            return []

        fused = reciprocal_rank_fusion(candidate_lists, top_k=top_k)

        results: list[RetrievalResult] = []
        for fc in fused:
            original = result_lookup.get(fc.doc_id)
            if original:
                results.append(RetrievalResult(
                    text=original.text, score=fc.score,
                    doc_id=fc.doc_id, citation=original.citation,
                ))

        return results

    async def _generate_variants(self, query: str) -> list[str]:
        if self._llm is None:
            return self._heuristic_variants(query)

        try:
            prompt = _VARIANT_PROMPT.format(n=self._cfg.num_variants, query=query)
            result = await self._llm(prompt)
            if not result:
                return self._heuristic_variants(query)

            text = result if isinstance(result, str) else str(result)
            lines = [
                ln.strip().lstrip("0123456789.-) ")
                for ln in text.strip().splitlines()
                if ln.strip()
            ]
            return lines[:self._cfg.num_variants]
        except Exception as exc:
            logger.warning("LLM variant generation failed: %s", exc)
            return self._heuristic_variants(query)

    @staticmethod
    def _heuristic_variants(query: str) -> list[str]:
        words = query.split()
        variants = []
        if len(words) > 3:
            variants.append(" ".join(words[1:]))
            variants.append(" ".join(words[:-1]))
        return variants
