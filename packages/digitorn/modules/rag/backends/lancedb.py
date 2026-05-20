"""LanceDB vector backend - embedded, columnar, zero-config."""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import CollectionInfo, Document, SearchResult, VectorBackend

logger = logging.getLogger(__name__)

class LanceDBBackend(VectorBackend):

    supports_sparse = False
    supports_hybrid = False
    supports_metadata_filter = True

    def __init__(self, path: str = ".lancedb") -> None:
        self._path = path
        self._db: Any = None
        self._meta: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        import lancedb
        self._db = lancedb.connect(self._path)
        logger.info("LanceDB backend initialized at %s", self._path)

    async def close(self) -> None:
        self._db = None

    async def create_collection(
        self, name: str, dimension: int, metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._db is None:
            raise RuntimeError("Backend not initialized")

        if name in self._db.table_names():
            self._meta[name] = {"dimension": dimension, **(metadata or {})}
            return

        import pyarrow as pa
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("doc_id", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dimension)),
            pa.field("metadata", pa.string()),
            pa.field("added_at", pa.float64()),
        ])
        self._db.create_table(name, schema=schema)
        self._meta[name] = {"dimension": dimension, "created_at": time.time(), **(metadata or {})}

    async def delete_collection(self, name: str) -> bool:
        if self._db is None:
            return False
        try:
            self._db.drop_table(name)
            self._meta.pop(name, None)
            return True
        except Exception:
            return False

    async def list_collections(self) -> list[CollectionInfo]:
        if self._db is None:
            return []
        result = []
        for name in self._db.table_names():
            meta = self._meta.get(name, {})
            try:
                tbl = self._db.open_table(name)
                count = tbl.count_rows()
            except Exception:
                count = 0
            result.append(CollectionInfo(
                name=name, dimension=meta.get("dimension", 0),
                doc_count=count, metadata=meta,
            ))
        return result

    async def collection_exists(self, name: str) -> bool:
        if self._db is None:
            return False
        return name in self._db.table_names()

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> int:
        if self._db is None:
            raise RuntimeError("Backend not initialized")

        import json
        tbl = self._db.open_table(collection)
        now = time.time()
        rows = []
        for i, (doc_id, vector, text) in enumerate(zip(ids, vectors, texts)):
            meta = (metadatas[i] if metadatas and i < len(metadatas) else {})
            rows.append({
                "id": doc_id,
                "text": text,
                "doc_id": doc_id,
                "vector": vector,
                "metadata": json.dumps(meta),
                "added_at": now,
            })

        tbl.add(rows)
        return len(rows)

    async def delete(self, collection: str, ids: list[str]) -> int:
        if self._db is None:
            return 0
        tbl = self._db.open_table(collection)
        deleted = 0
        for doc_id in ids:
            try:
                tbl.delete(f"doc_id = '{doc_id}'")
                deleted += 1
            except Exception as exc:
                logger.debug("lancedb best-effort block failed: %s", exc)
        return deleted

    async def get(self, collection: str, ids: list[str]) -> list[Document]:
        if self._db is None:
            return []

        import json
        tbl = self._db.open_table(collection)
        docs = []
        for doc_id in ids:
            try:
                rows = tbl.search().where(f"doc_id = '{doc_id}'").limit(1).to_list()
                if rows:
                    row = rows[0]
                    meta = json.loads(row.get("metadata", "{}")) if isinstance(row.get("metadata"), str) else {}
                    docs.append(Document(doc_id=doc_id, text=row.get("text", ""), metadata=meta))
            except Exception as exc:
                logger.debug("lancedb best-effort block failed: %s", exc)
        return docs

    async def count(self, collection: str) -> int:
        if self._db is None:
            return 0
        try:
            return self._db.open_table(collection).count_rows()
        except Exception:
            return 0

    async def get_all(self, collection: str) -> list[dict[str, Any]]:
        if self._db is None:
            return []

        import json
        tbl = self._db.open_table(collection)
        rows = tbl.to_pandas()
        out = []
        for _, row in rows.iterrows():
            meta = json.loads(row.get("metadata", "{}")) if isinstance(row.get("metadata"), str) else {}
            out.append({"id": row.get("doc_id", ""), "text": row.get("text", ""), "metadata": meta})
        return out

    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if self._db is None:
            return []

        import json
        tbl = self._db.open_table(collection)
        query = tbl.search(vector).limit(top_k).metric("cosine")

        if filter:
            conditions = []
            for k, v in filter.items():
                if isinstance(v, str):
                    conditions.append(f"metadata LIKE '%\"{k}\": \"{v}\"%'")
            if conditions:
                query = query.where(" AND ".join(conditions))

        rows = query.to_list()
        results = []
        for row in rows:
            distance = row.get("_distance", 1.0)
            score = 1.0 - distance
            if score < min_score:
                continue
            meta = json.loads(row.get("metadata", "{}")) if isinstance(row.get("metadata"), str) else {}
            results.append(SearchResult(
                doc_id=row.get("doc_id", ""),
                text=row.get("text", ""),
                score=score,
                metadata=meta,
            ))
        return results

    def state_snapshot(self) -> dict[str, Any]:
        return {"meta": self._meta}

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._meta = state.get("meta", {})
