"""Result fusion - RRF and weighted merging across retrieval sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass(slots=True)
class FusionCandidate:
    doc_id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""

def reciprocal_rank_fusion(
    result_lists: list[list[FusionCandidate]],
    k: int = 60,
    top_k: int | None = None,
) -> list[FusionCandidate]:
    scores: dict[str, float] = {}
    payloads: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}

    for results in result_lists:
        for rank, c in enumerate(results):
            scores[c.doc_id] = scores.get(c.doc_id, 0.0) + 1.0 / (k + rank + 1)
            if c.doc_id not in payloads:
                payloads[c.doc_id] = c.payload
                sources[c.doc_id] = c.source

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if top_k is not None:
        ranked = ranked[:top_k]

    return [
        FusionCandidate(did, sc, payloads.get(did, {}), sources.get(did, ""))
        for did, sc in ranked
    ]

def weighted_fusion(
    result_lists: list[tuple[list[FusionCandidate], float]],
    top_k: int | None = None,
) -> list[FusionCandidate]:
    scores: dict[str, float] = {}
    payloads: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}

    for results, weight in result_lists:
        if not results:
            continue
        max_score = max(c.score for c in results) or 1.0
        for c in results:
            norm = c.score / max_score
            scores[c.doc_id] = scores.get(c.doc_id, 0.0) + norm * weight
            if c.doc_id not in payloads:
                payloads[c.doc_id] = c.payload
                sources[c.doc_id] = c.source

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    if top_k is not None:
        ranked = ranked[:top_k]

    return [
        FusionCandidate(did, sc, payloads.get(did, {}), sources.get(did, ""))
        for did, sc in ranked
    ]
