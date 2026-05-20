"""Qdrant vector backend - default, zero-config, embedded mode."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .base import CollectionInfo, Document, SearchResult, VectorBackend

logger = logging.getLogger(__name__)

# Lazy import
_QdrantClient = None
_models = None

def _ensure_qdrant() -> tuple[Any, Any]:
    global _QdrantClient, _models
    if _QdrantClient is None:
        from qdrant_client import QdrantClient, models
        _QdrantClient = QdrantClient
        _models = models
    return _QdrantClient, _models

class QdrantBackend(VectorBackend):
    """Qdrant embedded vector backend."""

    supports_sparse = True
    supports_hybrid = False  # We use BM25 + RRF instead of Qdrant sparse vectors for now
    supports_metadata_filter = True

    def __init__(
        self,
        path: str = "",
        url: str = "",
        quantization: str = "none",
    ) -> None:
        self._path = path or None
        self._url = url or None
        self._quantization = quantization
        self._client: Any = None
        self._collection_meta: dict[str, dict[str, Any]] = {}
        self._next_point_id: int = 0

    async def initialize(self) -> None:
        # Off-load the heavy `qdrant_client` import + construction.
        QdrantClient, _ = await asyncio.to_thread(_ensure_qdrant)
        if self._url:
            self._client = await asyncio.to_thread(
                QdrantClient, url=self._url,
            )
            logger.info("Qdrant backend connected to %s", self._url)
        elif self._path:
            try:
                self._client = await asyncio.to_thread(
                    QdrantClient, path=self._path,
                )
                logger.info("Qdrant backend using persistent storage: %s", self._path)
            except RuntimeError as exc:
                msg = str(exc)
                if "already accessed" not in msg and "AlreadyLocked" not in msg:
                    raise
                logger.warning(
                    "Qdrant storage %s is locked by another instance - "
                    "falling back to in-memory. Collections from disk will NOT "
                    "be available. Run one daemon per storage folder, or "
                    "override DIGITORN_RAG_PATH to isolate this instance.",
                    self._path,
                )
                self._client = await asyncio.to_thread(
                    QdrantClient, ":memory:",
                )
                self._path = None
        else:
            self._client = await asyncio.to_thread(
                QdrantClient, ":memory:",
            )
            logger.info("Qdrant backend using in-memory mode")

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    async def create_collection(
        self, name: str, dimension: int, metadata: dict[str, Any] | None = None,
    ) -> None:
        _, models = _ensure_qdrant()
        if self._client is None:
            raise RuntimeError("Backend not initialized")

        try:
            self._client.get_collection(name)
            # Already exists
            return
        except Exception as exc:
            logger.debug("qdrant best-effort block failed: %s", exc)

        quantization_config = None
        if self._quantization == "int8":
            quantization_config = models.ScalarQuantization(
                scalar=models.ScalarQuantizationConfig(
                    type=models.ScalarType.INT8,
                    always_ram=True,
                ),
            )
        elif self._quantization == "binary":
            quantization_config = models.BinaryQuantization(
                binary=models.BinaryQuantizationConfig(always_ram=True),
            )

        self._client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(
                size=dimension,
                distance=models.Distance.COSINE,
            ),
            quantization_config=quantization_config,
        )

        self._collection_meta[name] = {
            "dimension": dimension,
            "created_at": time.time(),
            "doc_count": 0,
            **(metadata or {}),
        }

    async def delete_collection(self, name: str) -> bool:
        if self._client is None:
            return False
        try:
            self._client.delete_collection(name)
            self._collection_meta.pop(name, None)
            return True
        except Exception:
            return False

    async def list_collections(self) -> list[CollectionInfo]:
        if self._client is None:
            return []
        collections = self._client.get_collections().collections
        result = []
        for c in collections:
            meta = self._collection_meta.get(c.name, {})
            info = self._client.get_collection(c.name)
            result.append(CollectionInfo(
                name=c.name,
                dimension=meta.get("dimension", 0),
                doc_count=info.points_count or 0,
                description=meta.get("description", ""),
                embedding_model=meta.get("embedding_model", ""),
                metadata=meta,
            ))
        return result

    async def collection_exists(self, name: str) -> bool:
        if self._client is None:
            return False
        try:
            self._client.get_collection(name)
            return True
        except Exception:
            return False

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> int:
        _, models = _ensure_qdrant()
        if self._client is None:
            raise RuntimeError("Backend not initialized")

        if metadatas is None:
            metadatas = [{}] * len(ids)

        points = []
        for doc_id, vector, text, meta in zip(ids, vectors, texts, metadatas):
            point_id = self._next_point_id
            self._next_point_id += 1
            payload = {
                "text": text,
                "doc_id": doc_id,
                "added_at": time.time(),
                **meta,
            }
            points.append(models.PointStruct(id=point_id, vector=vector, payload=payload))

        self._client.upsert(collection_name=collection, points=points)

        if collection in self._collection_meta:
            self._collection_meta[collection]["doc_count"] = (
                self._client.get_collection(collection).points_count or 0
            )

        return len(points)

    async def delete(self, collection: str, ids: list[str]) -> int:
        _, models = _ensure_qdrant()
        if self._client is None:
            return 0

        # Find point IDs by doc_id payload filter
        deleted = 0
        for doc_id in ids:
            try:
                self._client.delete(
                    collection_name=collection,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[models.FieldCondition(
                                key="doc_id",
                                match=models.MatchValue(value=doc_id),
                            )],
                        ),
                    ),
                )
                deleted += 1
            except Exception as exc:
                logger.debug("qdrant best-effort block failed: %s", exc)

        return deleted

    async def get(self, collection: str, ids: list[str]) -> list[Document]:
        _, models = _ensure_qdrant()
        if self._client is None:
            return []

        docs = []
        for doc_id in ids:
            results, _ = self._client.scroll(
                collection_name=collection,
                scroll_filter=models.Filter(
                    must=[models.FieldCondition(
                        key="doc_id",
                        match=models.MatchValue(value=doc_id),
                    )],
                ),
                limit=1,
                with_payload=True,
            )
            for point in results:
                payload = point.payload or {}
                docs.append(Document(
                    doc_id=doc_id,
                    text=payload.get("text", ""),
                    metadata={k: v for k, v in payload.items() if k not in ("text", "doc_id")},
                ))
        return docs

    async def count(self, collection: str) -> int:
        if self._client is None:
            return 0
        try:
            info = self._client.get_collection(collection)
            return info.points_count or 0
        except Exception:
            return 0

    async def get_all(self, collection: str) -> list[dict[str, Any]]:
        if self._client is None:
            return []

        all_docs: list[dict[str, Any]] = []
        offset = None

        while True:
            results, next_offset = self._client.scroll(
                collection_name=collection,
                limit=100,
                offset=offset,
                with_payload=True,
            )
            for point in results:
                payload = point.payload or {}
                all_docs.append({
                    "id": payload.get("doc_id", str(point.id)),
                    "text": payload.get("text", ""),
                    "metadata": {
                        k: v for k, v in payload.items()
                        if k not in ("text", "doc_id", "added_at")
                    },
                })
            if next_offset is None or not results:
                break
            offset = next_offset

        return all_docs

    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        _, models = _ensure_qdrant()
        if self._client is None:
            return []

        query_filter = None
        if filter:
            conditions = []
            for key, value in filter.items():
                conditions.append(models.FieldCondition(
                    key=key, match=models.MatchValue(value=value),
                ))
            query_filter = models.Filter(must=conditions)

        results = self._client.query_points(
            collection_name=collection,
            query=vector,
            limit=top_k,
            score_threshold=min_score if min_score > 0 else None,
            query_filter=query_filter,
            with_payload=True,
        ).points

        return [
            SearchResult(
                doc_id=p.payload.get("doc_id", str(p.id)) if p.payload else str(p.id),
                text=p.payload.get("text", "") if p.payload else "",
                score=p.score if p.score is not None else 0.0,
                metadata={
                    k: v for k, v in (p.payload or {}).items()
                    if k not in ("text", "doc_id")
                },
            )
            for p in results
        ]

    def state_snapshot(self) -> dict[str, Any]:
        return {
            "collection_meta": self._collection_meta,
            "next_point_id": self._next_point_id,
        }

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._collection_meta = state.get("collection_meta", {})
        self._next_point_id = state.get("next_point_id", 0)
