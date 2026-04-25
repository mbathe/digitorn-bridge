"""QueryRouter — classifies queries to determine optimal retrieval strategy.

Two stages:
1. Fast classifier (<5ms): regex heuristics + signal scoring
2. LLM fallback (optional, +300ms): when confidence is below threshold
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RoutePlan:
    strategy: str  # hybrid | semantic | bm25 | sql | adaptive
    sources: list[RouteSource] = field(default_factory=list)
    confidence: float = 1.0


@dataclass(slots=True)
class RouteSource:
    type: str       # vector | sql | web
    source_id: str  # knowledge base name or connection_id
    query: str = ""


_SQL_SIGNALS = re.compile(
    r"\b("
    r"combien|total|moyenne|somme|max|min|count|sum|average|how many|percentage"
    r"|dernier trimestre|last quarter|en \d{4}|between .+ and"
    r"|group by|order by|top \d+|least|most|highest|lowest"
    r"|revenue|sales|profit|cost|budget|growth|rate|ratio"
    r")\b",
    re.IGNORECASE,
)

_VECTOR_SIGNALS = re.compile(
    r"\b("
    r"politique|procedure|definition|explain|describe|comment fonctionne|what is"
    r"|how to|why|overview|summary|guidelines|best practice|documentation"
    r"|meaning|concept|principle|process|workflow"
    r")\b",
    re.IGNORECASE,
)

_HYBRID_SIGNALS = re.compile(
    r"\b("
    r"compare|difference entre|versus|correlation|relationship|impact"
    r"|based on .+ data|according to .+ and"
    r")\b",
    re.IGNORECASE,
)


class QueryRouter:
    """Routes queries to the best retrieval strategy."""

    def __init__(
        self,
        *,
        has_sql_sources: bool = False,
        confidence_threshold: float = 0.8,
    ) -> None:
        self._has_sql = has_sql_sources
        self._threshold = confidence_threshold

    def classify(self, query: str) -> RoutePlan:
        sql_score = len(_SQL_SIGNALS.findall(query))
        vec_score = len(_VECTOR_SIGNALS.findall(query))
        hyb_score = len(_HYBRID_SIGNALS.findall(query))

        total = sql_score + vec_score + hyb_score or 1

        if self._has_sql and sql_score > 0 and sql_score >= vec_score:
            confidence = sql_score / total
            if confidence >= self._threshold:
                return RoutePlan(strategy="sql", confidence=confidence)
            return RoutePlan(strategy="hybrid", confidence=confidence * 0.8)

        if hyb_score > 0:
            return RoutePlan(strategy="hybrid", confidence=hyb_score / total)

        if vec_score > sql_score:
            confidence = vec_score / total
            return RoutePlan(
                strategy="semantic" if confidence >= self._threshold else "hybrid",
                confidence=confidence,
            )

        return RoutePlan(strategy="hybrid", confidence=0.5)

    def route_with_sources(
        self,
        query: str,
        knowledge_bases: list[str],
        sql_connections: list[str] | None = None,
    ) -> RoutePlan:
        plan = self.classify(query)

        if plan.strategy == "sql" and sql_connections:
            plan.sources = [
                RouteSource(type="sql", source_id=cid, query=query)
                for cid in sql_connections
            ]
        else:
            plan.sources = [
                RouteSource(type="vector", source_id=kb, query=query)
                for kb in knowledge_bases
            ]

        return plan
