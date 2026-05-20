"""Cross-encoder re-ranker - FastEmbed TextCrossEncoder, auto-download."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BUILTIN_RERANKERS: dict[str, str] = {
    "minilm-l6": "Xenova/ms-marco-MiniLM-L-6-v2",
    "minilm-l12": "Xenova/ms-marco-MiniLM-L-12-v2",
    "bge-reranker-base": "BAAI/bge-reranker-base",
    "jina-reranker-v1-tiny": "jinaai/jina-reranker-v1-tiny-en",
    "jina-reranker-v2": "jinaai/jina-reranker-v2-base-multilingual",
}

DEFAULT_RERANKER = "minilm-l6"

class CrossEncoderReranker:
    """Singleton cross-encoder reranker with lazy model loading."""

    def __init__(
        self,
        model: str = "",
        cache_dir: str | Path | None = None,
    ) -> None:
        self._model_id = self._resolve(model or DEFAULT_RERANKER)
        if cache_dir is None:
            from digitorn.core.paths import models_dir
            cache_dir = models_dir()
        self._cache_dir = Path(cache_dir)
        self._encoder: Any = None

    @staticmethod
    def _resolve(spec: str) -> str:
        if spec in BUILTIN_RERANKERS:
            return BUILTIN_RERANKERS[spec]
        return spec

    def _load(self) -> Any:
        if self._encoder is not None:
            return self._encoder
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:
            raise RuntimeError(
                "fastembed >= 0.3 required for cross-encoder reranking"
            ) from exc

        logger.info("Loading reranker %s", self._model_id)
        self._encoder = TextCrossEncoder(
            self._model_id,
            cache_dir=str(self._cache_dir),
        )
        return self._encoder

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[tuple[int, float]]:
        """Re-score documents. Returns [(original_index, score)] sorted descending."""
        if not documents:
            return []

        encoder = self._load()
        pairs = [(query, doc) for doc in documents]
        scores = list(encoder.rerank(query, documents))

        indexed: list[tuple[int, float]] = []
        for entry in scores:
            idx = entry.index if hasattr(entry, "index") else entry.get("index", 0)
            score = entry.score if hasattr(entry, "score") else entry.get("score", 0.0)
            indexed.append((idx, float(score)))

        indexed.sort(key=lambda x: x[1], reverse=True)
        if top_k is not None:
            indexed = indexed[:top_k]
        return indexed

    @staticmethod
    def list_builtin() -> dict[str, str]:
        return dict(BUILTIN_RERANKERS)
