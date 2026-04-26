"""Semantic embeddings via fastembed.

Default model: ``sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2``
(384-dim, ~50 languages including FR/EN). No special prefix convention.
"""
from __future__ import annotations

import asyncio
import threading
from functools import lru_cache

from ..settings import get_settings


class _ModelHolder:
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls):
        if cls._instance is not None:
            return cls._instance
        with cls._lock:
            if cls._instance is not None:
                return cls._instance
            from fastembed import TextEmbedding  # type: ignore[import-not-found]

            model_name = get_settings().embedding_model
            cls._instance = TextEmbedding(model_name=model_name)
            return cls._instance


def _embed_sync(text: str) -> list[float]:
    model = _ModelHolder.get()
    vector = next(iter(model.embed([text])))
    return [float(x) for x in vector]


async def embed_query(text: str) -> list[float]:
    text = (text or "").strip()
    if not text:
        return []
    return await asyncio.to_thread(_embed_sync, text)


async def embed_document(name: str, description: str, tags: list[str]) -> list[float]:
    parts = [name.strip()]
    if description:
        parts.append(description.strip())
    if tags:
        parts.append(" ".join(tags))
    body = " || ".join(p for p in parts if p)
    if not body:
        return []
    return await asyncio.to_thread(_embed_sync, body)


@lru_cache(maxsize=1)
def warmup() -> None:
    """Trigger model load. Call once at startup if you want to absorb the cost."""
    _ModelHolder.get()
