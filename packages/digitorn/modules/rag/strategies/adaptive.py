"""AdaptiveStrategy — routes queries to the best strategy based on complexity.

Uses the QueryRouter to classify queries, then delegates to the appropriate
sub-strategy (semantic, hybrid, sql, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

from ..citations import RetrievalResult
from ..router import QueryRouter
from .base import RetrievalStrategy

logger = logging.getLogger(__name__)


class AdaptiveStrategy(RetrievalStrategy):

    def __init__(
        self,
        strategies: dict[str, RetrievalStrategy],
        router: QueryRouter,
        default: str = "hybrid",
    ) -> None:
        self._strategies = strategies
        self._router = router
        self._default = default

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[RetrievalResult]:
        plan = self._router.classify(query)
        strategy_name = plan.strategy

        if strategy_name not in self._strategies:
            strategy_name = self._default

        strategy = self._strategies.get(strategy_name)
        if strategy is None:
            logger.warning("No strategy found for %r, using first available", strategy_name)
            strategy = next(iter(self._strategies.values()), None)
            if strategy is None:
                return []

        logger.debug(
            "Adaptive routing: %r -> %s (confidence=%.2f)",
            query[:50], strategy_name, plan.confidence,
        )

        return await strategy.retrieve(
            query, top_k=top_k, min_score=min_score,
            metadata_filter=metadata_filter, **kwargs,
        )
