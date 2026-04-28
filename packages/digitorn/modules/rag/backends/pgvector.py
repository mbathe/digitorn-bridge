"""pgvector backend - PostgreSQL with vector extension.

Requires: pip install asyncpg pgvector
Uses the database module's connection via ServiceBus, or a direct DSN.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .base import CollectionInfo, Document, SearchResult, VectorBackend

logger = logging.getLogger(__name__)


class PgvectorBackend(VectorBackend):

    supports_sparse = False
    supports_hybrid = False
    supports_metadata_filter = True

    def __init__(self, dsn: str = "") -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._meta: dict[str, dict[str, Any]] = {}

    async def initialize(self) -> None:
        import asyncpg
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        async with self._pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        logger.info("pgvector backend initialized")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def create_collection(
        self, name: str, dimension: int, metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._pool is None:
            raise RuntimeError("Backend not initialized")

        safe_name = name.replace("'", "").replace('"', "").replace(";", "")
        async with self._pool.acquire() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS "{safe_name}" (
                    id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    metadata JSONB DEFAULT '{{}}',
                    embedding vector({dimension}),
                    added_at TIMESTAMPTZ DEFAULT now()
                )
            """)
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS "{safe_name}_embedding_idx"
                ON "{safe_name}" USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
            """)
        self._meta[name] = {"dimension": dimension, "created_at": time.time(), **(metadata or {})}

    async def delete_collection(self, name: str) -> bool:
        if self._pool is None:
            return False
        safe_name = name.replace("'", "").replace('"', "").replace(";", "")
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(f'DROP TABLE IF EXISTS "{safe_name}"')
            self._meta.pop(name, None)
            return True
        except Exception:
            return False

    async def list_collections(self) -> list[CollectionInfo]:
        return [
            CollectionInfo(name=n, dimension=m.get("dimension", 0), metadata=m)
            for n, m in self._meta.items()
        ]

    async def collection_exists(self, name: str) -> bool:
        if self._pool is None:
            return False
        safe_name = name.replace("'", "").replace('"', "").replace(";", "")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM information_schema.tables WHERE table_name = $1", safe_name,
            )
            return row is not None

    async def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> int:
        if self._pool is None:
            raise RuntimeError("Backend not initialized")

        safe_name = collection.replace("'", "").replace('"', "").replace(";", "")
        async with self._pool.acquire() as conn:
            for i, (doc_id, vector, text) in enumerate(zip(ids, vectors, texts)):
                meta = metadatas[i] if metadatas and i < len(metadatas) else {}
                vec_str = "[" + ",".join(str(v) for v in vector) + "]"
                await conn.execute(f"""
                    INSERT INTO "{safe_name}" (id, doc_id, text, metadata, embedding)
                    VALUES ($1, $2, $3, $4::jsonb, $5::vector)
                    ON CONFLICT (id) DO UPDATE SET
                        text = EXCLUDED.text,
                        metadata = EXCLUDED.metadata,
                        embedding = EXCLUDED.embedding,
                        added_at = now()
                """, doc_id, doc_id, text, json.dumps(meta), vec_str)

        return len(ids)

    async def delete(self, collection: str, ids: list[str]) -> int:
        if self._pool is None:
            return 0
        safe_name = collection.replace("'", "").replace('"', "").replace(";", "")
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f'DELETE FROM "{safe_name}" WHERE doc_id = ANY($1)', ids,
            )
            return int(result.split()[-1]) if result else 0

    async def get(self, collection: str, ids: list[str]) -> list[Document]:
        if self._pool is None:
            return []
        safe_name = collection.replace("'", "").replace('"', "").replace(";", "")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f'SELECT doc_id, text, metadata FROM "{safe_name}" WHERE doc_id = ANY($1)', ids,
            )
        docs = []
        for row in rows:
            meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"] or {})
            docs.append(Document(doc_id=row["doc_id"], text=row["text"], metadata=meta))
        return docs

    async def count(self, collection: str) -> int:
        if self._pool is None:
            return 0
        safe_name = collection.replace("'", "").replace('"', "").replace(";", "")
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(f'SELECT count(*) FROM "{safe_name}"')
                return row[0] if row else 0
        except Exception:
            return 0

    async def get_all(self, collection: str) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        safe_name = collection.replace("'", "").replace('"', "").replace(";", "")
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(f'SELECT doc_id, text, metadata FROM "{safe_name}"')
        out = []
        for row in rows:
            meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"] or {})
            out.append({"id": row["doc_id"], "text": row["text"], "metadata": meta})
        return out

    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if self._pool is None:
            return []

        safe_name = collection.replace("'", "").replace('"', "").replace(";", "")
        vec_str = "[" + ",".join(str(v) for v in vector) + "]"

        where_clauses = []
        params: list[Any] = [vec_str, top_k]

        if filter:
            for k, v in filter.items():
                safe_key = k.replace("'", "")
                where_clauses.append(f"metadata->>'{safe_key}' = ${len(params) + 1}")
                params.append(str(v))

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        query = f"""
            SELECT doc_id, text, metadata,
                   1 - (embedding <=> $1::vector) AS score
            FROM "{safe_name}"
            {where_sql}
            ORDER BY embedding <=> $1::vector
            LIMIT $2
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        results = []
        for row in rows:
            score = float(row["score"])
            if score < min_score:
                continue
            meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"] or {})
            results.append(SearchResult(
                doc_id=row["doc_id"], text=row["text"], score=score, metadata=meta,
            ))
        return results

    def state_snapshot(self) -> dict[str, Any]:
        return {"meta": self._meta}

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._meta = state.get("meta", {})
