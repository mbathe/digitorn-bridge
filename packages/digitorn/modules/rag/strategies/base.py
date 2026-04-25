"""Base protocol for retrieval strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..citations import RetrievalResult


class RetrievalStrategy(ABC):

    @abstractmethod
    async def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float = 0.0,
        metadata_filter: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[RetrievalResult]:
        ...
