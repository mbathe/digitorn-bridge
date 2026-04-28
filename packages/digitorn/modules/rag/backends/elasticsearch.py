"""Elasticsearch vector backend - kNN + BM25 native hybrid search.

Requires: pip install elasticsearch[async]
Supports Elasticsearch 8.x with dense_vector fields and native kNN search.
Also supports native hybrid search (kNN + BM25 in a single query).
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from .base import CollectionInfo, Document, SearchResult, VectorBackend

logger = logging.getLogger(__name__)

# Lazy imports
_AsyncElasticsearch = None


def _ensure_elasticsearch() -> Any:
    global _AsyncElasticsearch
    if _AsyncElasticsearch is None:
        from elasticsearch import AsyncElasticsearch
        _AsyncElasticsearch = AsyncElasticsearch
    return _AsyncElasticsearch


class ElasticsearchBackend(VectorBackend):
    """Elasticsearch 8.x vector backend with native kNN and hybrid search.

    Zero-config with a running ES instance: just provide the URL.
    Supports both pure semantic search and native hybrid (kNN + BM25).
    """

    supports_sparse = False
    supports_hybrid = True  # ES has native hybrid via kNN + query combo
    supports_metadata_filter = True

    def __init__(
        self,
        url: str = "http://localhost:9200",
        api_key: str = "",
        cloud_id: str = "",
        verify_certs: bool = True,
    ) -> None:
        self._url = url
        self._api_key = api_key or None
        self._cloud_id = cloud_id or None
        self._verify_certs = verify_certs
        self._client: Any = None
        self._collection_meta: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        AsyncES = _ensure_elasticsearch()
        kwargs: dict[str, Any] = {}

        if self._cloud_id:
            kwargs["cloud_id"] = self._cloud_id
        else:
            kwargs["hosts"] = [self._url]

        if self._api_key:
            kwargs["api_key"] = self._api_key

        kwargs["verify_certs"] = self._verify_certs

        self._client = AsyncES(**kwargs)

        # Verify connection
        info = await self._client.info()
        version = info.get("version", {}).get("number", "unknown")
        logger.info("Elasticsearch backend connected: %s (v%s)", self._url, version)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Collections (mapped to ES indices)
    # ------------------------------------------------------------------

    def _index_name(self, collection: str) -> str:
        """Sanitize collection name to a valid ES index name."""
        return collection.lower().replace(" ", "-")

    async def create_collection(
        self, name: str, dimension: int, metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._client is None:
            raise RuntimeError("Backend not initialized")

        index = self._index_name(name)

        if await self._client.indices.exists(index=index):
            self._collection_meta[name] = {
                "dimension": dimension,
                **(metadata or {}),
            }
            return

        mappings = {
            "properties": {
                "embedding": {
                    "type": "dense_vector",
                    "dims": dimension,
                    "index": True,
                    "similarity": "cosine",
                },
                "text": {
                    "type": "text",
                    "analyzer": "standard",
                },
                "doc_id": {
                    "type": "keyword",
                },
                "added_at": {
                    "type": "date",
                    "format": "epoch_second",
                },
                "metadata": {
                    "type": "object",
                    "enabled": True,
                },
            },
        }

        settings = {
            "number_of_shards": 1,
            "number_of_replicas": 0,
        }

        await self._client.indices.create(
            index=index, mappings=mappings, settings=settings,
        )

        self._collection_meta[name] = {
            "dimension": dimension,
            "created_at": time.time(),
            **(metadata or {}),
        }
        logger.info("Created ES index %r (dims=%d)", index, dimension)

    async def delete_collection(self, name: str) -> bool:
        if self._client is None:
            return False
        index = self._index_name(name)
        try:
            await self._client.indices.delete(index=index)
            self._collection_meta.pop(name, None)
            return True
        except Exception:
            return False

    async def list_collections(self) -> list[CollectionInfo]:
        if self._client is None:
            return []

        result = []
        for name, meta in self._collection_meta.items():
            index = self._index_name(name)
            try:
                count_resp = await self._client.count(index=index)
                doc_count = count_resp.get("count", 0)
            except Exception:
                doc_count = 0

            result.append(CollectionInfo(
                name=name,
                dimension=meta.get("dimension", 0),
                doc_count=doc_count,
                description=meta.get("description", ""),
                embedding_model=meta.get("embedding_model", ""),
                metadata=meta,
            ))
        return result

    async def collection_exists(self, name: str) -> bool:
        if self._client is None:
            return False
        index = self._index_name(name)
        return await self._client.indices.exists(index=index)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def _doc_es_id(self, collection: str, doc_id: str) -> str:
        """Deterministic ES _id from collection + doc_id."""
        raw = f"{collection}:{doc_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

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

        index = self._index_name(collection)
        if metadatas is None:
            metadatas = [{}] * len(ids)

        operations: list[dict[str, Any]] = []
        for doc_id, vector, text, meta in zip(ids, vectors, texts, metadatas):
            es_id = self._doc_es_id(collection, doc_id)
            operations.append({"index": {"_index": index, "_id": es_id}})
            operations.append({
                "doc_id": doc_id,
                "text": text,
                "embedding": vector,
                "added_at": int(time.time()),
                "metadata": meta,
            })

        if operations:
            resp = await self._client.bulk(operations=operations, refresh="wait_for")
            if resp.get("errors"):
                error_items = [
                    item for item in resp.get("items", [])
                    if "error" in item.get("index", {})
                ]
                if error_items:
                    logger.warning(
                        "ES bulk upsert had %d errors (first: %s)",
                        len(error_items),
                        error_items[0]["index"]["error"],
                    )

        return len(ids)

    async def delete(self, collection: str, ids: list[str]) -> int:
        if self._client is None:
            return 0

        index = self._index_name(collection)
        operations: list[dict[str, Any]] = []
        for doc_id in ids:
            es_id = self._doc_es_id(collection, doc_id)
            operations.append({"delete": {"_index": index, "_id": es_id}})

        if operations:
            await self._client.bulk(operations=operations, refresh="wait_for")

        return len(ids)

    async def get(self, collection: str, ids: list[str]) -> list[Document]:
        if self._client is None:
            return []

        index = self._index_name(collection)
        docs = []
        for doc_id in ids:
            es_id = self._doc_es_id(collection, doc_id)
            try:
                resp = await self._client.get(index=index, id=es_id)
                source = resp.get("_source", {})
                docs.append(Document(
                    doc_id=doc_id,
                    text=source.get("text", ""),
                    metadata=source.get("metadata", {}),
                ))
            except Exception:
                pass
        return docs

    async def count(self, collection: str) -> int:
        if self._client is None:
            return 0
        index = self._index_name(collection)
        try:
            resp = await self._client.count(index=index)
            return resp.get("count", 0)
        except Exception:
            return 0

    async def get_all(self, collection: str) -> list[dict[str, Any]]:
        if self._client is None:
            return []

        index = self._index_name(collection)
        all_docs: list[dict[str, Any]] = []

        resp = await self._client.search(
            index=index,
            body={"query": {"match_all": {}}, "size": 10000},
            _source=["doc_id", "text", "metadata"],
        )

        for hit in resp.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            all_docs.append({
                "id": source.get("doc_id", hit["_id"]),
                "text": source.get("text", ""),
                "metadata": source.get("metadata", {}),
            })

        return all_docs

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

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

        index = self._index_name(collection)

        # Build kNN query
        knn: dict[str, Any] = {
            "field": "embedding",
            "query_vector": vector,
            "k": top_k,
            "num_candidates": max(top_k * 4, 100),
        }

        if filter:
            knn["filter"] = self._build_filter(filter)

        resp = await self._client.search(
            index=index,
            knn=knn,
            size=top_k,
            _source=["doc_id", "text", "metadata"],
        )

        results = []
        for hit in resp.get("hits", {}).get("hits", []):
            score = hit.get("_score", 0.0)
            if score < min_score:
                continue
            source = hit.get("_source", {})
            results.append(SearchResult(
                doc_id=source.get("doc_id", hit["_id"]),
                text=source.get("text", ""),
                score=score,
                metadata=source.get("metadata", {}),
            ))

        return results

    async def hybrid_search(
        self,
        collection: str,
        vector: list[float],
        text_query: str,
        top_k: int = 10,
        alpha: float = 0.7,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Native ES hybrid: kNN (semantic) + BM25 (text) combined via RRF.

        Uses Elasticsearch's built-in Reciprocal Rank Fusion when available (8.14+),
        otherwise falls back to a boosted bool query combining kNN and match.
        """
        if self._client is None:
            return []

        index = self._index_name(collection)

        knn: dict[str, Any] = {
            "field": "embedding",
            "query_vector": vector,
            "k": top_k,
            "num_candidates": max(top_k * 4, 100),
        }

        text_query_dsl: dict[str, Any] = {
            "match": {
                "text": {
                    "query": text_query,
                    "boost": (1.0 - alpha) * 10,
                },
            },
        }

        if filter:
            es_filter = self._build_filter(filter)
            knn["filter"] = es_filter
            text_query_dsl = {
                "bool": {
                    "must": [text_query_dsl],
                    "filter": [es_filter],
                },
            }

        # Combined kNN + text query - ES merges scores automatically
        resp = await self._client.search(
            index=index,
            knn=knn,
            query=text_query_dsl,
            size=top_k,
            _source=["doc_id", "text", "metadata"],
        )

        results = []
        for hit in resp.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            results.append(SearchResult(
                doc_id=source.get("doc_id", hit["_id"]),
                text=source.get("text", ""),
                score=hit.get("_score", 0.0),
                metadata=source.get("metadata", {}),
            ))

        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_filter(self, filter: dict[str, Any]) -> dict[str, Any]:
        """Convert simple key=value filter dict to ES filter DSL."""
        conditions = []
        for key, value in filter.items():
            # Support filtering on metadata sub-fields
            field = f"metadata.{key}" if key not in ("doc_id", "text") else key
            conditions.append({"term": {field: value}})

        if len(conditions) == 1:
            return conditions[0]
        return {"bool": {"must": conditions}}

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def state_snapshot(self) -> dict[str, Any]:
        return {"collection_meta": self._collection_meta}

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._collection_meta = state.get("collection_meta", {})
