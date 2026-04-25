"""Pinecone vector backend — cloud-hosted, serverless.

Requires: pip install pinecone
Cloud-only. Requires API key + index name in config.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import CollectionInfo, Document, SearchResult, VectorBackend

logger = logging.getLogger(__name__)


class PineconeBackend(VectorBackend):

    supports_sparse = True
    supports_hybrid = True
    supports_metadata_filter = True

    def __init__(
        self,
        api_key: str = "",
        index_name: str = "",
        cloud: str = "aws",
        region: str = "us-east-1",
    ) -> None:
        self._api_key = api_key
        self._index_name = index_name
        self._cloud = cloud
        self._region = region
        self._pc: Any = None
        self._index: Any = None
        self._meta: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        from pinecone import Pinecone
        self._pc = Pinecone(api_key=self._api_key)
        if self._index_name:
            self._index = self._pc.Index(self._index_name)
        logger.info("Pinecone backend initialized (index=%s)", self._index_name)

    async def close(self) -> None:
        self._index = None
        self._pc = None

    async def create_collection(
        self, name: str, dimension: int, metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._pc is None:
            raise RuntimeError("Backend not initialized")

        from pinecone import ServerlessSpec

        existing = [idx.name for idx in self._pc.list_indexes()]
        ns_name = name.replace("_", "-")

        if ns_name not in existing:
            self._pc.create_index(
                name=ns_name, dimension=dimension, metric="cosine",
                spec=ServerlessSpec(cloud=self._cloud, region=self._region),
            )

        self._index = self._pc.Index(ns_name)
        self._meta[name] = {"dimension": dimension, "index": ns_name, **(metadata or {})}

    async def delete_collection(self, name: str) -> bool:
        if self._pc is None:
            return False
        try:
            ns_name = self._meta.get(name, {}).get("index", name.replace("_", "-"))
            self._pc.delete_index(ns_name)
            self._meta.pop(name, None)
            return True
        except Exception:
            return False

    async def list_collections(self) -> list[CollectionInfo]:
        if self._pc is None:
            return []
        return [
            CollectionInfo(
                name=name, dimension=meta.get("dimension", 0), metadata=meta,
            )
            for name, meta in self._meta.items()
        ]

    async def collection_exists(self, name: str) -> bool:
        return name in self._meta

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> int:
        if self._index is None:
            raise RuntimeError("No index connected")

        records = []
        for i, (doc_id, vector, text) in enumerate(zip(ids, vectors, texts)):
            meta = (metadatas[i] if metadatas and i < len(metadatas) else {}).copy()
            meta["text"] = text[:40000]
            meta["doc_id"] = doc_id
            records.append({"id": doc_id, "values": vector, "metadata": meta})

        batch_size = 100
        for i in range(0, len(records), batch_size):
            self._index.upsert(vectors=records[i:i + batch_size], namespace=collection)

        return len(records)

    async def delete(self, collection: str, ids: list[str]) -> int:
        if self._index is None:
            return 0
        self._index.delete(ids=ids, namespace=collection)
        return len(ids)

    async def get(self, collection: str, ids: list[str]) -> list[Document]:
        if self._index is None:
            return []

        result = self._index.fetch(ids=ids, namespace=collection)
        docs = []
        for doc_id, vec_data in (result.get("vectors") or {}).items():
            meta = vec_data.get("metadata", {})
            docs.append(Document(
                doc_id=meta.get("doc_id", doc_id),
                text=meta.get("text", ""),
                metadata={k: v for k, v in meta.items() if k not in ("text", "doc_id")},
            ))
        return docs

    async def count(self, collection: str) -> int:
        if self._index is None:
            return 0
        try:
            stats = self._index.describe_index_stats()
            ns_stats = stats.get("namespaces", {}).get(collection, {})
            return ns_stats.get("vector_count", 0)
        except Exception:
            return 0

    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if self._index is None:
            return []

        result = self._index.query(
            vector=vector, top_k=top_k, namespace=collection,
            include_metadata=True, filter=filter,
        )

        results = []
        for match in result.get("matches", []):
            score = match.get("score", 0.0)
            if score < min_score:
                continue
            meta = match.get("metadata", {})
            results.append(SearchResult(
                doc_id=meta.get("doc_id", match.get("id", "")),
                text=meta.get("text", ""),
                score=score,
                metadata={k: v for k, v in meta.items() if k not in ("text", "doc_id")},
            ))
        return results

    def state_snapshot(self) -> dict[str, Any]:
        return {"meta": self._meta}

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._meta = state.get("meta", {})
