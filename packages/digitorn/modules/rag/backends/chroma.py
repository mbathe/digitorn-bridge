"""ChromaDB vector backend.

Requires: pip install chromadb
Supports in-memory (default) and persistent (path-based) modes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import CollectionInfo, Document, SearchResult, VectorBackend

logger = logging.getLogger(__name__)


class ChromaBackend(VectorBackend):

    supports_sparse = False
    supports_hybrid = False
    supports_metadata_filter = True

    def __init__(self, path: str = "", url: str = "") -> None:
        self._path = path or None
        self._url = url or None
        self._client: Any = None
        self._collections: dict[str, Any] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        import chromadb

        if self._url:
            self._client = chromadb.HttpClient(host=self._url)
        elif self._path:
            self._client = chromadb.PersistentClient(path=self._path)
        else:
            self._client = chromadb.Client()
        logger.info("ChromaDB backend initialized")

    async def close(self) -> None:
        self._client = None
        self._collections.clear()

    async def create_collection(
        self, name: str, dimension: int, metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._client is None:
            raise RuntimeError("Backend not initialized")
        coll = self._client.get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"},
        )
        self._collections[name] = coll
        self._meta[name] = {"dimension": dimension, "created_at": time.time(), **(metadata or {})}

    async def delete_collection(self, name: str) -> bool:
        if self._client is None:
            return False
        try:
            self._client.delete_collection(name)
            self._collections.pop(name, None)
            self._meta.pop(name, None)
            return True
        except Exception:
            return False

    async def list_collections(self) -> list[CollectionInfo]:
        if self._client is None:
            return []
        colls = self._client.list_collections()
        result = []
        for c in colls:
            meta = self._meta.get(c.name, {})
            result.append(CollectionInfo(
                name=c.name, dimension=meta.get("dimension", 0),
                doc_count=c.count(), metadata=meta,
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

    def _get_coll(self, name: str) -> Any:
        if name not in self._collections:
            self._collections[name] = self._client.get_collection(name)
        return self._collections[name]

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> int:
        if self._client is None:
            raise RuntimeError("Backend not initialized")

        coll = self._get_coll(collection)
        safe_meta = None
        if metadatas:
            safe_meta = []
            for m in metadatas:
                safe = {k: v for k, v in m.items() if isinstance(v, (str, int, float, bool))}
                # ChromaDB rejects empty metadata dicts
                safe_meta.append(safe if safe else {"_p": True})

        coll.upsert(ids=ids, embeddings=vectors, documents=texts, metadatas=safe_meta)
        return len(ids)

    async def delete(self, collection: str, ids: list[str]) -> int:
        if self._client is None:
            return 0
        coll = self._get_coll(collection)
        coll.delete(ids=ids)
        return len(ids)

    async def get(self, collection: str, ids: list[str]) -> list[Document]:
        if self._client is None:
            return []
        coll = self._get_coll(collection)
        result = coll.get(ids=ids, include=["documents", "metadatas"])
        docs = []
        for i, doc_id in enumerate(result.get("ids", [])):
            docs.append(Document(
                doc_id=doc_id,
                text=(result.get("documents") or [""])[i] if result.get("documents") else "",
                metadata=(result.get("metadatas") or [{}])[i] if result.get("metadatas") else {},
            ))
        return docs

    async def count(self, collection: str) -> int:
        if self._client is None:
            return 0
        try:
            return self._get_coll(collection).count()
        except Exception:
            return 0

    async def get_all(self, collection: str) -> list[dict[str, Any]]:
        if self._client is None:
            return []
        coll = self._get_coll(collection)
        result = coll.get(include=["documents", "metadatas"])
        out = []
        for i, doc_id in enumerate(result.get("ids", [])):
            out.append({
                "id": doc_id,
                "text": (result.get("documents") or [""])[i] if result.get("documents") else "",
                "metadata": (result.get("metadatas") or [{}])[i] if result.get("metadatas") else {},
            })
        return out

    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if self._client is None:
            return []

        coll = self._get_coll(collection)
        where = None
        if filter:
            conditions = {k: v for k, v in filter.items() if isinstance(v, (str, int, float, bool))}
            if conditions:
                where = conditions if len(conditions) == 1 else {"$and": [{k: v} for k, v in conditions.items()]}

        result = coll.query(
            query_embeddings=[vector], n_results=top_k,
            where=where, include=["documents", "metadatas", "distances"],
        )

        results = []
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        for i, doc_id in enumerate(ids):
            score = 1.0 - distances[i] if i < len(distances) else 0.0
            if score < min_score:
                continue
            results.append(SearchResult(
                doc_id=doc_id,
                text=docs[i] if i < len(docs) else "",
                score=score,
                metadata=metas[i] if i < len(metas) else {},
            ))
        return results

    def state_snapshot(self) -> dict[str, Any]:
        return {"meta": self._meta}

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._meta = state.get("meta", {})
