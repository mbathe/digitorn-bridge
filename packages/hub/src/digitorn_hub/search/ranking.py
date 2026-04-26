from __future__ import annotations

import uuid
from collections import defaultdict


def reciprocal_rank_fusion(
    rankings: list[list[uuid.UUID]],
    *,
    k: int = 60,
) -> list[tuple[uuid.UUID, float]]:
    """Combine multiple ranked id lists using RRF.

    `score(d) = sum over rankers of 1 / (k + rank(d))`. Higher = better.
    Returns the merged list sorted by score descending.
    """
    scores: dict[uuid.UUID, float] = defaultdict(float)
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
