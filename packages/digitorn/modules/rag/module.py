"""RAG module - optional, only loaded when declared in app YAML."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from digitorn.modules.base import ActionResult, BaseModule, Platform
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ConstraintSpec, ModuleManifest

from .bm25 import BM25Index
from .cache import SemanticCache
from .citations import RetrievalResult, format_context_block
from .config import RagConfig
from .embeddings import EmbeddingManager, ResolvedModel
from .indexing.engine import IndexingEngine
from .router import QueryRouter
from .params import (
    ClearCacheParams,
    CreateKnowledgeBaseParams,
    DeleteKnowledgeBaseParams,
    IngestDatabaseParams,
    IngestDirectoryParams,
    IngestFileParams,
    IngestParams,
    KnowledgeBaseStatsParams,
    ListKnowledgeBasesParams,
    ListModelsParams,
    MigrateEmbeddingsParams,
    MultiQueryParams,
    QueryParams,
    SQLQueryParams,
)
from .pipeline import RagPipeline
from .reranker import CrossEncoderReranker

logger = logging.getLogger(__name__)


class _KBMeta:
    __slots__ = (
        "name", "description", "embedding_model", "vector_dim",
        "created_at", "doc_count", "chunk_count",
        "bm25", "content_hashes",
    )

    def __init__(
        self,
        name: str,
        description: str = "",
        embedding_model: str = "",
        vector_dim: int = 384,
    ) -> None:
        self.name = name
        self.description = description
        self.embedding_model = embedding_model
        self.vector_dim = vector_dim
        self.created_at = time.time()
        self.doc_count = 0
        self.chunk_count = 0
        self.bm25 = BM25Index()
        self.content_hashes: dict[str, str] = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "embedding_model": self.embedding_model,
            "vector_dim": self.vector_dim,
            "created_at": self.created_at,
            "doc_count": self.doc_count,
            "chunk_count": self.chunk_count,
            "content_hashes": dict(self.content_hashes),
            "bm25": self.bm25.to_dict(),
        }


class RagModule(BaseModule):
    MODULE_ID = "rag"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    CONFIG_MODEL = RagConfig

    CONSTRAINTS = [
        ConstraintSpec(
            name="max_knowledge_bases", type="integer",
            description="Maximum number of knowledge bases.",
            scope="module", default=50,
        ),
        ConstraintSpec(
            name="max_documents", type="integer",
            description="Maximum total documents across all knowledge bases.",
            scope="module", default=100000,
        ),
        ConstraintSpec(
            name="paths", type="string_list",
            description="Allowed path prefixes for file ingestion.",
            scope="module", applies_to=["ingest_file", "ingest_directory"],
        ),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cfg: RagConfig = RagConfig()
        self._embedding_mgr: EmbeddingManager | None = None
        self._default_model: ResolvedModel | None = None
        self._backend: Any = None
        self._pipeline: RagPipeline | None = None
        self._reranker: CrossEncoderReranker | None = None
        self._cache: SemanticCache | None = None
        self._router: QueryRouter | None = None
        self._indexing: IndexingEngine | None = None
        self._kbs: dict[str, _KBMeta] = {}
        self._app_id: str = "default"

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def on_start(self) -> None:
        self._app_id = getattr(self, "_app_id_override", "default")

        if self._config:
            self._cfg = (
                RagConfig.model_validate(self._config)
                if isinstance(self._config, dict) else self._config
            )
        else:
            self._cfg = RagConfig()

        self._embedding_mgr = EmbeddingManager()
        self._default_model = self._embedding_mgr.resolve(self._cfg.embedding_model)

        self._backend = await self._create_backend()
        await self._backend.initialize()

        if self._cfg.reranker:
            model_id = self._cfg.reranker if isinstance(self._cfg.reranker, str) else ""
            try:
                self._reranker = CrossEncoderReranker(model=model_id)
            except Exception:
                logger.warning("Failed to init reranker, continuing without", exc_info=True)

        if self._cfg.cache.enabled:
            self._cache = SemanticCache(
                config=self._cfg.cache,
                embedding_mgr=self._embedding_mgr,
                model=self._default_model,
            )

        has_sql = any(
            hasattr(s, "tables") for s in self._cfg.sources
        )
        self._router = QueryRouter(has_sql_sources=has_sql)

        self._pipeline = RagPipeline(
            backend=self._backend,
            embedding_mgr=self._embedding_mgr,
            config=self._cfg.pipeline,
            reranker=self._reranker,
            cache=self._cache,
        )

        bus = getattr(self, "_service_bus", None)
        self._indexing = IndexingEngine(service_bus=bus)

        await self._discover_existing_collections()

        if self._cfg.auto_index.on_start and self._cfg.sources:
            await self._auto_index_sources()

        logger.info(
            "RAG module started (app=%s model=%s dims=%d backend=%s reranker=%s kbs=%d)",
            self._app_id,
            self._default_model.fastembed_id,
            self._default_model.dimensions,
            type(self._backend).__name__,
            type(self._reranker).__name__ if self._reranker else "none",
            len(self._kbs),
        )

    async def on_config_update(self, config: dict[str, Any]) -> None:
        """Re-init the backend when the per-app config arrives.

        The base ``on_config_update`` only stores the config dict;
        the rag module needs to actually rebuild the qdrant client
        with the new path so per-app stores work even when the module
        is loaded as ``shared``.
        """
        if not isinstance(config, dict):
            return
        new_cfg = RagConfig.model_validate(config)
        old_backend_kind = type(self._backend).__name__ if self._backend else ""
        old_backend_path = ""
        if self._backend is not None:
            old_backend_path = getattr(self._backend, "_path", None) or ""
        new_backend_kind = (new_cfg.backend.type if hasattr(new_cfg, "backend") else "")
        new_backend_path = ""
        if hasattr(new_cfg, "backend") and hasattr(new_cfg.backend, "path"):
            new_backend_path = new_cfg.backend.path or ""

        backend_changed = (
            old_backend_path != new_backend_path
            or (old_backend_kind == "" and new_backend_kind != "")
        )

        self._config = config
        self._cfg = new_cfg

        if backend_changed:
            logger.info(
                "rag module: backend reconfigured (old=%s new=%s) - recreating client",
                old_backend_path or "<default>", new_backend_path or "<default>",
            )
            if self._backend is not None:
                try:
                    await self._backend.close()
                except Exception as exc:
                    logger.warning("rag old backend close failed: %s", exc)
            self._backend = await self._create_backend()
            await self._backend.initialize()
            if self._pipeline is not None:
                self._pipeline = RagPipeline(
                    backend=self._backend,
                    embedding_mgr=self._embedding_mgr,
                    config=self._cfg.pipeline,
                    reranker=self._reranker,
                    cache=self._cache,
                )
            self._kbs.clear()
            await self._discover_existing_collections()

    async def _discover_existing_collections(self) -> None:
        """Rebuild ``_kbs`` metadata from collections already on disk.

        Collections may exist independently of the daemon's lifecycle:
        ``knowledge_base/build.py`` (and other offline tools) write
        directly to the qdrant store. Without this scan, the in-memory
        ``_kbs`` would stay empty after a daemon restart and every
        ``query`` would return "Knowledge base not found" even though
        the data is right there.
        """
        if self._backend is None:
            return
        prefix = f"rag_{self._app_id}_"
        try:
            collections = await self._backend.list_collections()
        except Exception as exc:
            logger.warning("rag discovery: list_collections failed: %s", exc)
            return
        discovered = 0
        for coll in collections:
            if not coll.name.startswith(prefix):
                continue
            kb_name = coll.name[len(prefix):]
            if kb_name in self._kbs:
                continue
            self._kbs[kb_name] = _KBMeta(
                name=kb_name,
                description=f"(auto-discovered from existing collection {coll.name})",
                embedding_model=self._default_model.fastembed_id if self._default_model else "",
                vector_dim=coll.dimension,
            )
            self._kbs[kb_name].chunk_count = coll.doc_count
            discovered += 1
        if discovered:
            logger.info(
                "rag discovery: re-attached %d existing kb(s) for app=%s",
                discovered, self._app_id,
            )

    async def on_stop(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
        if self._embedding_mgr is not None:
            try:
                close_fn = getattr(self._embedding_mgr, "close", None)
                if close_fn is not None:
                    res = close_fn()
                    if hasattr(res, "__await__"):
                        await res
            except Exception:
                pass
            self._embedding_mgr = None
        self._pipeline = None
        self._reranker = None
        self._cache = None
        self._router = None
        self._indexing = None
        self._kbs.clear()

    async def _create_backend(self) -> Any:
        cfg = self._cfg.backend

        if cfg.type == "qdrant":
            from .backends.qdrant import QdrantBackend
            return QdrantBackend(path=cfg.path, url=cfg.url, quantization=cfg.quantization)

        if cfg.type == "chroma":
            from .backends.chroma import ChromaBackend
            return ChromaBackend(path=cfg.path, url=cfg.url)

        if cfg.type == "lancedb":
            from .backends.lancedb import LanceDBBackend
            return LanceDBBackend(path=cfg.path or ".lancedb")

        if cfg.type == "pinecone":
            from .backends.pinecone import PineconeBackend
            return PineconeBackend(
                api_key=cfg.api_key, index_name=cfg.index_name,
                cloud=cfg.cloud, region=cfg.region,
            )

        if cfg.type == "pgvector":
            from .backends.pgvector import PgvectorBackend
            return PgvectorBackend(dsn=cfg.dsn)

        if cfg.type == "elasticsearch":
            from .backends.elasticsearch import ElasticsearchBackend
            return ElasticsearchBackend(
                url=cfg.url or "http://localhost:9200",
                api_key=cfg.api_key,
            )

        logger.warning("Unknown backend %r, falling back to Qdrant", cfg.type)
        from .backends.qdrant import QdrantBackend
        return QdrantBackend(path=cfg.path, url=cfg.url, quantization=cfg.quantization)

    # ── Helpers ───────────────────────────────────────────────────────

    def _collection_name(self, kb_name: str) -> str:
        return f"rag_{self._app_id}_{kb_name}"

    def _resolve_model(self, model_override: str) -> ResolvedModel:
        if model_override and self._embedding_mgr:
            return self._embedding_mgr.resolve(model_override)
        if self._default_model is None:
            raise RuntimeError("Embedding manager not initialized")
        return self._default_model

    def _check_kb_exists(self, name: str) -> _KBMeta | None:
        return self._kbs.get(name)

    def _check_kb_limit(self) -> str | None:
        limit = self._cfg.max_knowledge_bases
        ctx = getattr(self, "_context", None)
        if ctx and hasattr(ctx, "constraints"):
            limit = ctx.constraints.get("max_knowledge_bases", limit)
        if len(self._kbs) >= limit:
            return f"Knowledge base limit reached ({limit})."
        return None

    def _check_doc_limit(self, adding: int = 0) -> str | None:
        limit = self._cfg.max_documents
        total = sum(kb.chunk_count for kb in self._kbs.values()) + adding
        if total > limit:
            return f"Document limit exceeded ({total}/{limit})."
        return None

    @staticmethod
    def _content_hash(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()[:16]

    def _chunk_text(
        self, text: str, strategy: str = "", size: int = 0, overlap: int = -1,
    ) -> list[Any]:
        from digitorn.modules.vector.chunking import chunk_text
        return chunk_text(
            text,
            strategy=strategy or self._cfg.chunking.strategy,
            size=size or self._cfg.chunking.size,
            overlap=overlap if overlap >= 0 else self._cfg.chunking.overlap,
        )

    def _format_results(self, results: list[RetrievalResult]) -> list[dict[str, Any]]:
        out = []
        for r in results:
            d: dict[str, Any] = {
                "text": r.text, "score": round(r.score, 4), "doc_id": r.doc_id,
            }
            if self._cfg.citations.enabled:
                d["citation"] = {
                    "source_type": r.citation.source_type,
                    "source_id": r.citation.source_id,
                    "location": r.citation.location,
                    "confidence": round(r.citation.confidence, 3),
                }
            out.append(d)
        return out

    async def _get_llm_caller(self) -> Any:
        bus = getattr(self, "_service_bus", None)
        if bus is None:
            return None

        provider_id = self._cfg.text2sql.provider or ""
        if not provider_id:
            mq = self._cfg.pipeline.multi_query
            if mq:
                provider_id = mq.provider

        if not provider_id:
            return None

        async def _call_llm(prompt: str) -> str:
            result = await bus.call("llm_provider", "complete", {
                "provider_id": provider_id,
                "prompt": prompt,
                "max_tokens": 1024,
            })
            if result and result.success and result.data:
                return result.data.get("text", "")
            return ""

        return _call_llm

    async def _get_schema_text(self, connection_id: str, knowledge_base: str = "") -> str:
        bus = getattr(self, "_service_bus", None)
        if bus is None:
            return ""

        if knowledge_base:
            kb = self._check_kb_exists(knowledge_base)
            if kb:
                hits = kb.bm25.search("database schema table columns", top_k=10)
                if hits:
                    doc_ids = [did for did, _ in hits]
                    coll = self._collection_name(knowledge_base)
                    docs = await self._backend.get(coll, doc_ids)
                    if docs:
                        return "\n\n".join(d.text for d in docs)

        try:
            result = await bus.call("database", "introspect", {
                "connection_id": connection_id,
            })
            if result and result.success and result.data:
                tables = result.data.get("tables", [])
                lines = []
                for t in tables:
                    name = t.get("name", "?")
                    cols = t.get("columns", [])
                    col_strs = [f"  {c.get('name', '?')} {c.get('type', '?')}" for c in cols]
                    lines.append(f"Table: {name}\n" + "\n".join(col_strs))
                return "\n\n".join(lines)
        except Exception as exc:
            logger.warning("Schema introspection failed: %s", exc)

        return ""

    async def _auto_index_sources(self) -> None:
        from .config import DatabaseSourceConfig, FileSourceConfig

        for source in self._cfg.sources:
            try:
                if isinstance(source, FileSourceConfig):
                    await self._auto_index_file_source(source)
                elif isinstance(source, DatabaseSourceConfig):
                    await self._auto_index_db_source(source)
            except Exception as exc:
                logger.warning("Auto-index source failed: %s", exc, exc_info=True)

    async def _auto_index_file_source(self, source: Any) -> None:
        if self._indexing is None:
            return

        kb_name = "default"
        if kb_name not in self._kbs:
            await self.execute("create_knowledge_base", {"name": kb_name})

        docs = await self._indexing.scan_directory(
            source.path, source.extensions,
            recursive=source.recursive, max_files=source.max_files,
        )
        if not docs:
            return

        kb = self._kbs.get(kb_name)
        if kb is None:
            return

        model = self._resolve_model(kb.embedding_model)
        coll = self._collection_name(kb_name)

        for doc in docs:
            chunks = self._chunk_text(doc.text)
            if not chunks:
                continue
            chunk_texts = [c.text for c in chunks]
            chunk_ids = [f"{doc.doc_id}:chunk:{c.index}" for c in chunks]
            vectors = self._embedding_mgr.embed(chunk_texts, model)  # type: ignore[union-attr]
            metadatas = [{**doc.metadata, "chunk_index": c.index} for c in chunks]

            await self._backend.upsert(
                collection=coll, ids=chunk_ids, vectors=vectors,
                texts=chunk_texts, metadatas=metadatas,
            )
            kb.bm25.add_documents(chunk_ids, chunk_texts)
            kb.chunk_count += len(chunks)

        kb.doc_count += len(docs)
        logger.info("Auto-indexed %d documents from %s", len(docs), source.path)

    async def _auto_index_db_source(self, source: Any) -> None:
        if self._indexing is None:
            return

        kb_name = "default"
        if kb_name not in self._kbs:
            await self.execute("create_knowledge_base", {"name": kb_name})

        tables_dict = {}
        for table_name, table_cfg in source.tables.items():
            tables_dict[table_name] = {
                "columns": table_cfg.columns,
                "mode": table_cfg.mode,
                "template": table_cfg.template,
                "max_rows": table_cfg.max_rows,
            }

        await self.execute("ingest_database", {
            "knowledge_base": kb_name,
            "connection_id": source.connection_id,
            "tables": tables_dict,
        })

        for table_name, table_cfg in source.tables.items():
            sync_strategy = table_cfg.sync or source.sync.strategy
            if table_cfg.mode == "embed_rows" and sync_strategy:
                await self._indexing.setup_db_sync(
                    source.connection_id, table_name, sync_strategy,
                    auto_triggers=source.sync.auto_create_triggers,
                )

        logger.info(
            "Auto-indexed database %s (%d tables)",
            source.connection_id, len(source.tables),
        )

    # ── Actions ───────────────────────────────────────────────────────

    @action(
        description="Create a new knowledge base for storing and searching documents.",
        params_model=CreateKnowledgeBaseParams,
        tags=["rag", "knowledge-base"],
        risk_level="low",
        cli_label="Create KB",
        cli_param="name",
    )
    async def create_knowledge_base(self, params: CreateKnowledgeBaseParams) -> ActionResult:
        err = self._check_kb_limit()
        if err:
            return ActionResult(success=False, error=err)

        if params.name in self._kbs:
            return ActionResult(success=True, data={
                "knowledge_base": params.name, "created": False, "already_exists": True,
            })

        model = self._resolve_model(params.embedding_model)
        coll = self._collection_name(params.name)

        await self._backend.create_collection(
            name=coll, dimension=model.dimensions,
            metadata={"embedding_model": model.fastembed_id, "description": params.description},
        )

        self._kbs[params.name] = _KBMeta(
            name=params.name, description=params.description,
            embedding_model=model.fastembed_id, vector_dim=model.dimensions,
        )

        return ActionResult(success=True, data={
            "knowledge_base": params.name, "created": True,
            "embedding_model": model.fastembed_id, "dimensions": model.dimensions,
        })

    @action(
        description="Delete a knowledge base and all its data.",
        params_model=DeleteKnowledgeBaseParams,
        tags=["rag", "knowledge-base"],
        risk_level="high",
        irreversible=True,
        cli_label="Delete KB",
        cli_param="name",
    )
    async def delete_knowledge_base(self, params: DeleteKnowledgeBaseParams) -> ActionResult:
        kb = self._check_kb_exists(params.name)
        if kb is None:
            return ActionResult(success=False, error=f"Knowledge base '{params.name}' not found.")

        await self._backend.delete_collection(self._collection_name(params.name))
        del self._kbs[params.name]
        return ActionResult(success=True, data={"knowledge_base": params.name, "deleted": True})

    @action(
        description="List all knowledge bases with their stats.",
        params_model=ListKnowledgeBasesParams,
        tags=["rag", "knowledge-base"],
        risk_level="low",
        cli_label="List KBs",
    )
    async def list_knowledge_bases(self, params: ListKnowledgeBasesParams) -> ActionResult:
        kbs = [
            {
                "name": kb.name, "description": kb.description,
                "embedding_model": kb.embedding_model, "dimensions": kb.vector_dim,
                "doc_count": kb.doc_count, "chunk_count": kb.chunk_count,
                "bm25_terms": kb.bm25.term_count, "created_at": kb.created_at,
            }
            for kb in self._kbs.values()
        ]
        return ActionResult(success=True, data={"knowledge_bases": kbs, "count": len(kbs)})

    @action(
        description="Get detailed statistics for a knowledge base.",
        params_model=KnowledgeBaseStatsParams,
        tags=["rag", "knowledge-base"],
        risk_level="low",
        cli_label="KB stats",
        cli_param="name",
    )
    async def knowledge_base_stats(self, params: KnowledgeBaseStatsParams) -> ActionResult:
        kb = self._check_kb_exists(params.name)
        if kb is None:
            return ActionResult(success=False, error=f"Knowledge base '{params.name}' not found.")

        backend_count = await self._backend.count(self._collection_name(params.name))
        data: dict[str, Any] = {
            "name": kb.name, "description": kb.description,
            "embedding_model": kb.embedding_model, "dimensions": kb.vector_dim,
            "doc_count": kb.doc_count, "chunk_count": kb.chunk_count,
            "backend_count": backend_count,
            "bm25_terms": kb.bm25.term_count, "bm25_docs": kb.bm25.doc_count,
            "content_hashes": len(kb.content_hashes), "created_at": kb.created_at,
            "backend_type": self._cfg.backend.type,
            "pipeline": {
                "retrieval": self._cfg.pipeline.retrieval,
                "reranker": type(self._reranker).__name__ if self._reranker else "none",
                "bm25_weight": self._cfg.pipeline.bm25_weight,
                "semantic_weight": self._cfg.pipeline.semantic_weight,
                "rerank_top_n": self._cfg.pipeline.rerank_top_n,
                "final_top_k": self._cfg.pipeline.final_top_k,
            },
        }
        if self._cache:
            stats = self._cache.stats
            data["cache"] = {
                "entries": stats.entries,
                "total_queries": stats.total_queries,
                "hit_rate": round(stats.hit_rate, 3),
                "hits": stats.cache_hits,
                "misses": stats.cache_misses,
                "evictions": stats.evictions,
            }
        return ActionResult(success=True, data=data)

    @action(
        description="Ingest raw text documents into a knowledge base.",
        params_model=IngestParams,
        tags=["rag", "ingest"],
        risk_level="low",
        cli_label="Ingest",
        cli_param="knowledge_base",
    )
    async def ingest(self, params: IngestParams) -> ActionResult:
        kb = self._check_kb_exists(params.knowledge_base)
        if kb is None:
            return ActionResult(
                success=False,
                error=f"Knowledge base '{params.knowledge_base}' not found. Create it first.",
            )

        err = self._check_doc_limit(len(params.documents))
        if err:
            return ActionResult(success=False, error=err)

        model = self._resolve_model(kb.embedding_model)
        coll = self._collection_name(params.knowledge_base)

        ids = params.ids or [
            hashlib.md5(f"{params.knowledge_base}:{i}:{doc[:64]}".encode()).hexdigest()[:16]
            for i, doc in enumerate(params.documents)
        ]

        vectors = self._embedding_mgr.embed(params.documents, model)  # type: ignore[union-attr]

        metadatas = []
        for i, _doc in enumerate(params.documents):
            meta = (params.metadata[i] if params.metadata and i < len(params.metadata) else {}).copy()
            meta["source_type"] = params.source_type
            meta["source_id"] = params.source_id or "manual"
            metadatas.append(meta)

        added = await self._backend.upsert(
            collection=coll, ids=ids, vectors=vectors,
            texts=params.documents, metadatas=metadatas,
        )

        kb.bm25.add_documents(ids, params.documents)
        kb.doc_count += len(params.documents)
        kb.chunk_count += len(params.documents)

        return ActionResult(success=True, data={
            "knowledge_base": params.knowledge_base, "added": added, "ids": ids,
        })

    @action(
        description="Ingest a file into a knowledge base with automatic chunking.",
        params_model=IngestFileParams,
        tags=["rag", "ingest"],
        risk_level="low",
        cli_label="Ingest file",
        cli_param="path",
    )
    async def ingest_file(self, params: IngestFileParams) -> ActionResult:
        kb = self._check_kb_exists(params.knowledge_base)
        if kb is None:
            return ActionResult(
                success=False,
                error=f"Knowledge base '{params.knowledge_base}' not found. Create it first.",
            )

        file_path = Path(params.path).resolve()
        if not file_path.exists():
            return ActionResult(success=False, error=f"File not found: {params.path}")
        if not file_path.is_file():
            return ActionResult(success=False, error=f"Not a file: {params.path}")

        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ActionResult(success=False, error=f"Cannot read file: {e}")

        if not text.strip():
            return ActionResult(success=True, data={
                "knowledge_base": params.knowledge_base,
                "file": str(file_path), "chunks": 0, "added": 0, "skipped": "empty file",
            })

        content_hash = self._content_hash(text)
        source_key = str(file_path)
        if kb.content_hashes.get(source_key) == content_hash:
            return ActionResult(success=True, data={
                "knowledge_base": params.knowledge_base,
                "file": str(file_path), "chunks": 0, "added": 0,
                "skipped": "unchanged",
            })

        old_ids = [
            did for did in list(kb.bm25._docs.keys())
            if did.startswith(f"file:{source_key}:")
        ]
        if old_ids:
            await self._backend.delete(self._collection_name(params.knowledge_base), old_ids)
            kb.bm25.remove_documents(old_ids)
            kb.chunk_count -= len(old_ids)

        chunks = self._chunk_text(text, params.chunk_strategy, params.chunk_size, params.chunk_overlap)
        if not chunks:
            return ActionResult(success=True, data={
                "knowledge_base": params.knowledge_base,
                "file": str(file_path), "chunks": 0, "added": 0,
            })

        err = self._check_doc_limit(len(chunks))
        if err:
            return ActionResult(success=False, error=err)

        model = self._resolve_model(kb.embedding_model)
        coll = self._collection_name(params.knowledge_base)

        chunk_texts = [c.text for c in chunks]
        chunk_ids = [f"file:{source_key}:{c.index}" for c in chunks]
        vectors = self._embedding_mgr.embed(chunk_texts, model)  # type: ignore[union-attr]

        metadatas = [
            {
                "source_type": "file", "source_id": source_key,
                "chunk_index": c.index, "start_char": c.start_char, "end_char": c.end_char,
                **(params.metadata or {}),
            }
            for c in chunks
        ]

        added = await self._backend.upsert(
            collection=coll, ids=chunk_ids, vectors=vectors,
            texts=chunk_texts, metadatas=metadatas,
        )

        kb.bm25.add_documents(chunk_ids, chunk_texts)
        kb.content_hashes[source_key] = content_hash
        kb.doc_count += 1
        kb.chunk_count += len(chunks)

        return ActionResult(success=True, data={
            "knowledge_base": params.knowledge_base,
            "file": str(file_path), "chunks": len(chunks), "added": added,
            "strategy": params.chunk_strategy or self._cfg.chunking.strategy,
        })

    @action(
        description="Search a knowledge base using the configured retrieval pipeline with citations.",
        params_model=QueryParams,
        tags=["rag", "search"],
        risk_level="low",
        cli_label="Query KB",
        cli_param="query",
    )
    async def query(self, params: QueryParams) -> ActionResult:
        kb = self._check_kb_exists(params.knowledge_base)
        if kb is None:
            return ActionResult(
                success=False, error=f"Knowledge base '{params.knowledge_base}' not found.",
            )

        if self._pipeline is None:
            return ActionResult(success=False, error="RAG pipeline not initialized.")

        if self._cache is not None:
            cached = self._cache.lookup(params.query)
            if cached is not None:
                result_dicts, context_block = cached
                data: dict[str, Any] = {
                    "knowledge_base": params.knowledge_base,
                    "query": params.query,
                    "strategy": "cache_hit",
                    "results": result_dicts,
                    "count": len(result_dicts),
                    "cache_hit": True,
                }
                if context_block:
                    data["context_block"] = context_block
                return ActionResult(success=True, data=data)

        model = self._resolve_model(kb.embedding_model)
        coll = self._collection_name(params.knowledge_base)

        effective_strategy = params.strategy
        if effective_strategy == "adaptive" and self._router:
            plan = self._router.classify(params.query)
            effective_strategy = plan.strategy if plan.strategy in ("semantic", "bm25", "hybrid") else "hybrid"

        results = await self._pipeline.retrieve(
            query=params.query,
            collection=coll,
            model=model,
            bm25=kb.bm25,
            top_k=params.top_k,
            min_score=params.min_score,
            strategy=effective_strategy,
            metadata_filter=params.filter,
        )

        if self._cfg.crag.enabled and results:
            from .strategies.crag import CRAGStrategy
            from .strategies.base import RetrievalStrategy as _RS

            class _Passthrough(_RS):
                def __init__(self, r: list) -> None:
                    self._r = r
                async def retrieve(self, query: str, **kw: Any) -> list:
                    return self._r

            llm_caller = await self._get_llm_caller() if self._cfg.crag.provider else None
            crag = CRAGStrategy(
                _Passthrough(results),
                llm_caller=llm_caller,
                confidence_threshold=self._cfg.crag.confidence_threshold,
                fallback=self._cfg.crag.fallback,
            )
            results = await crag.retrieve(params.query, top_k=params.top_k)

        result_dicts = self._format_results(results)
        context_block = format_context_block(results) if self._cfg.citations.enabled and results else ""

        used_strategy = effective_strategy or self._cfg.pipeline.retrieval
        if self._cfg.crag.enabled:
            used_strategy += "+crag"

        data = {
            "knowledge_base": params.knowledge_base,
            "query": params.query,
            "strategy": used_strategy,
            "results": result_dicts,
            "count": len(result_dicts),
            "cache_hit": False,
        }

        if context_block:
            data["context_block"] = context_block

        if self._cache is not None and result_dicts:
            source_hashes = {
                r.citation.content_hash for r in results if r.citation.content_hash
            }
            self._cache.store(params.query, result_dicts, context_block, source_hashes)

        return ActionResult(success=True, data=data)

    @action(
        description="Search with LLM-generated query variants for broader recall (MultiQuery RAG).",
        params_model=MultiQueryParams,
        tags=["rag", "search", "advanced"],
        risk_level="low",
        cli_label="Multi query",
        cli_param="query",
    )
    async def multi_query(self, params: MultiQueryParams) -> ActionResult:
        kb = self._check_kb_exists(params.knowledge_base)
        if kb is None:
            return ActionResult(
                success=False, error=f"Knowledge base '{params.knowledge_base}' not found.",
            )

        if self._pipeline is None:
            return ActionResult(success=False, error="RAG pipeline not initialized.")

        from .strategies.multiquery import MultiQueryStrategy
        from .strategies.hybrid import HybridStrategy

        model = self._resolve_model(kb.embedding_model)
        coll = self._collection_name(params.knowledge_base)

        inner = HybridStrategy(
            backend=self._backend,
            embedding_mgr=self._embedding_mgr,
            model=model,
            bm25=kb.bm25,
            collection=coll,
            config=self._cfg.pipeline,
        )

        llm_caller = await self._get_llm_caller()
        mq_config = self._cfg.pipeline.multi_query
        if mq_config is None:
            from .config import MultiQueryConfig
            mq_config = MultiQueryConfig(enabled=True, num_variants=params.num_variants)

        strategy = MultiQueryStrategy(inner, mq_config, llm_caller=llm_caller)

        results = await strategy.retrieve(
            params.query, top_k=params.top_k, min_score=params.min_score,
            metadata_filter=params.filter,
        )

        if self._reranker and results:
            results = self._pipeline._apply_rerank(params.query, results, len(results))
            results = results[:params.top_k]

        result_dicts = self._format_results(results)
        context_block = format_context_block(results) if self._cfg.citations.enabled and results else ""

        data: dict[str, Any] = {
            "knowledge_base": params.knowledge_base,
            "query": params.query,
            "strategy": "multi_query",
            "num_variants": params.num_variants,
            "results": result_dicts,
            "count": len(result_dicts),
        }
        if context_block:
            data["context_block"] = context_block

        return ActionResult(success=True, data=data)

    @action(
        description="Answer a natural language question by generating and executing SQL.",
        params_model=SQLQueryParams,
        tags=["rag", "search", "database", "text2sql"],
        risk_level="medium",
        cli_label="SQL query",
        cli_param="query",
    )
    async def sql_query(self, params: SQLQueryParams) -> ActionResult:
        if self._pipeline is None:
            return ActionResult(success=False, error="RAG pipeline not initialized.")

        bus = getattr(self, "_service_bus", None)
        if bus is None:
            return ActionResult(
                success=False, error="ServiceBus not available - database module required.",
            )

        llm_caller = await self._get_llm_caller()
        if llm_caller is None:
            return ActionResult(
                success=False, error="LLM provider required for Text2SQL.",
            )

        schema_text = await self._get_schema_text(params.connection_id, params.knowledge_base)

        from .strategies.text2sql import Text2SQLStrategy

        strategy = Text2SQLStrategy(
            service_bus=bus,
            connection_id=params.connection_id,
            schema_text=schema_text,
            llm_caller=llm_caller,
        )

        results = await strategy.retrieve(params.query, top_k=params.top_k)

        result_dicts = self._format_results(results)
        context_block = format_context_block(results) if results else ""

        data: dict[str, Any] = {
            "query": params.query,
            "connection_id": params.connection_id,
            "strategy": "text2sql",
            "results": result_dicts,
            "count": len(result_dicts),
        }
        if context_block:
            data["context_block"] = context_block

        return ActionResult(success=True, data=data)

    @action(
        description="Ingest all matching files from a directory into a knowledge base.",
        params_model=IngestDirectoryParams,
        tags=["rag", "ingest"],
        risk_level="low",
        cli_label="Ingest dir",
        cli_param="path",
    )
    async def ingest_directory(self, params: IngestDirectoryParams) -> ActionResult:
        kb = self._check_kb_exists(params.knowledge_base)
        if kb is None:
            return ActionResult(
                success=False,
                error=f"Knowledge base '{params.knowledge_base}' not found. Create it first.",
            )

        if self._indexing is None:
            return ActionResult(success=False, error="Indexing engine not initialized.")

        dir_path = Path(params.path).resolve()
        if not dir_path.is_dir():
            return ActionResult(success=False, error=f"Directory not found: {params.path}")

        docs = await self._indexing.scan_directory(
            str(dir_path), params.extensions,
            recursive=params.recursive, max_files=params.max_files,
        )

        if not docs:
            return ActionResult(success=True, data={
                "knowledge_base": params.knowledge_base,
                "directory": str(dir_path), "files_processed": 0,
                "chunks": 0, "added": 0,
            })

        model = self._resolve_model(kb.embedding_model)
        coll = self._collection_name(params.knowledge_base)

        total_added = 0
        total_chunks = 0

        for doc in docs:
            chunks = self._chunk_text(
                doc.text, params.chunk_strategy, params.chunk_size, params.chunk_overlap,
            )
            if not chunks:
                continue

            err = self._check_doc_limit(len(chunks))
            if err:
                break

            chunk_texts = [c.text for c in chunks]
            chunk_ids = [f"{doc.doc_id}:chunk:{c.index}" for c in chunks]
            vectors = self._embedding_mgr.embed(chunk_texts, model)  # type: ignore[union-attr]

            metadatas = [
                {**doc.metadata, "chunk_index": c.index}
                for c in chunks
            ]

            added = await self._backend.upsert(
                collection=coll, ids=chunk_ids, vectors=vectors,
                texts=chunk_texts, metadatas=metadatas,
            )

            kb.bm25.add_documents(chunk_ids, chunk_texts)
            total_added += added
            total_chunks += len(chunks)

        kb.doc_count += len(docs)
        kb.chunk_count += total_chunks

        return ActionResult(success=True, data={
            "knowledge_base": params.knowledge_base,
            "directory": str(dir_path), "documents": len(docs),
            "chunks": total_chunks, "added": total_added,
        })

    @action(
        description="Ingest database tables into a knowledge base (schema and/or rows).",
        params_model=IngestDatabaseParams,
        tags=["rag", "ingest", "database"],
        risk_level="medium",
        cli_label="Ingest DB",
        cli_param="connection_string",
    )
    async def ingest_database(self, params: IngestDatabaseParams) -> ActionResult:
        kb = self._check_kb_exists(params.knowledge_base)
        if kb is None:
            return ActionResult(
                success=False,
                error=f"Knowledge base '{params.knowledge_base}' not found. Create it first.",
            )

        if self._indexing is None:
            return ActionResult(success=False, error="Indexing engine not initialized.")

        model = self._resolve_model(kb.embedding_model)
        coll = self._collection_name(params.knowledge_base)

        schema_docs = await self._indexing.index_database_schema(
            params.connection_id, params.tables,
        )

        row_docs = []
        for table_name, table_cfg in params.tables.items():
            mode = table_cfg.get("mode", "schema_only")
            if mode != "embed_rows":
                continue

            columns = table_cfg.get("columns", [])
            if not columns:
                continue

            template = table_cfg.get("template", "")
            max_rows = table_cfg.get("max_rows", 50000)

            docs = await self._indexing.index_database_rows(
                params.connection_id, table_name, columns,
                template=template, max_rows=max_rows,
            )
            row_docs.extend(docs)

        all_docs = schema_docs + row_docs
        if not all_docs:
            return ActionResult(success=True, data={
                "knowledge_base": params.knowledge_base,
                "connection_id": params.connection_id,
                "schema_docs": 0, "row_docs": 0, "added": 0,
            })

        texts = [d.text for d in all_docs]
        ids = [d.doc_id for d in all_docs]
        vectors = self._embedding_mgr.embed(texts, model)  # type: ignore[union-attr]
        metadatas = [d.metadata for d in all_docs]

        added = await self._backend.upsert(
            collection=coll, ids=ids, vectors=vectors,
            texts=texts, metadatas=metadatas,
        )

        kb.bm25.add_documents(ids, texts)
        kb.doc_count += len(all_docs)
        kb.chunk_count += len(all_docs)

        return ActionResult(success=True, data={
            "knowledge_base": params.knowledge_base,
            "connection_id": params.connection_id,
            "schema_docs": len(schema_docs), "row_docs": len(row_docs),
            "added": added,
        })

    @action(
        description="Clear the semantic cache for faster-but-stale response prevention.",
        params_model=ClearCacheParams,
        tags=["rag", "cache"],
        risk_level="low",
        cli_label="Clear cache",
    )
    async def clear_cache(self, params: ClearCacheParams) -> ActionResult:
        if self._cache is None:
            return ActionResult(success=True, data={
                "cleared": 0, "cache_enabled": False,
            })

        count = self._cache.clear(params.knowledge_base)
        stats = self._cache.stats
        return ActionResult(success=True, data={
            "cleared": count,
            "knowledge_base": params.knowledge_base or "all",
            "remaining_entries": stats.entries,
            "total_queries": stats.total_queries,
            "hit_rate": round(stats.hit_rate, 3),
        })

    @action(
        description="Re-embed a knowledge base with a different embedding model.",
        params_model=MigrateEmbeddingsParams,
        tags=["rag", "embeddings"],
        risk_level="high",
        cli_label="Migrate embeddings",
        cli_param="knowledge_base",
    )
    async def migrate_embeddings(self, params: MigrateEmbeddingsParams) -> ActionResult:
        kb = self._check_kb_exists(params.knowledge_base)
        if kb is None:
            return ActionResult(
                success=False,
                error=f"Knowledge base '{params.knowledge_base}' not found.",
            )

        if self._embedding_mgr is None:
            return ActionResult(success=False, error="Embedding manager not initialized.")

        target = self._embedding_mgr.resolve(params.target_model)
        if target.fastembed_id == kb.embedding_model:
            return ActionResult(success=True, data={
                "knowledge_base": params.knowledge_base,
                "migrated": False, "reason": "Already using target model.",
            })

        old_model = kb.embedding_model
        old_coll = self._collection_name(params.knowledge_base)
        all_points = await self._backend.get_all(old_coll)

        if not all_points:
            kb.embedding_model = target.fastembed_id
            kb.vector_dim = target.dimensions
            return ActionResult(success=True, data={
                "knowledge_base": params.knowledge_base,
                "migrated": True, "re_embedded": 0,
                "old_model": old_model, "new_model": target.fastembed_id,
            })

        texts = [p["text"] for p in all_points]
        ids = [p["id"] for p in all_points]
        metadatas = [p.get("metadata", {}) for p in all_points]

        await self._backend.delete_collection(old_coll)
        await self._backend.create_collection(
            name=old_coll, dimension=target.dimensions,
            metadata={"embedding_model": target.fastembed_id},
        )

        batch_size = 256
        for start in range(0, len(texts), batch_size):
            end = min(start + batch_size, len(texts))
            batch_vectors = self._embedding_mgr.embed(texts[start:end], target)
            await self._backend.upsert(
                collection=old_coll, ids=ids[start:end], vectors=batch_vectors,
                texts=texts[start:end], metadatas=metadatas[start:end],
            )

        kb.embedding_model = target.fastembed_id
        kb.vector_dim = target.dimensions

        if self._cache:
            self._cache.clear(params.knowledge_base)

        return ActionResult(success=True, data={
            "knowledge_base": params.knowledge_base,
            "migrated": True, "re_embedded": len(texts),
            "old_model": old_model,
            "new_model": target.fastembed_id,
            "new_dimensions": target.dimensions,
        })

    @action(
        description="List available embedding models (built-in shortcuts and current default).",
        params_model=ListModelsParams,
        tags=["rag", "embeddings"],
        risk_level="low",
        cli_label="List models",
    )
    async def list_models(self, params: ListModelsParams) -> ActionResult:
        from .embeddings import BUILTIN_MODELS

        models = []
        for shortcut, spec in BUILTIN_MODELS.items():
            models.append({
                "shortcut": shortcut,
                "fastembed_id": spec.fastembed_id,
                "dimensions": spec.dimensions,
                "description": spec.description,
                "is_default": (
                    self._default_model is not None
                    and spec.fastembed_id == self._default_model.fastembed_id
                ),
            })

        return ActionResult(success=True, data={
            "models": models,
            "current_default": self._default_model.fastembed_id if self._default_model else "",
            "current_dimensions": self._default_model.dimensions if self._default_model else 0,
        })

    # ── State ─────────────────────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {
            "app_id": self._app_id,
            "knowledge_bases": {n: kb.to_dict() for n, kb in self._kbs.items()},
            "backend": self._backend.state_snapshot() if self._backend else {},
            "indexing": self._indexing.state_snapshot() if self._indexing else {},
        }
        if self._cache:
            s = self._cache.stats
            snap["cache_stats"] = {
                "total_queries": s.total_queries,
                "cache_hits": s.cache_hits,
                "cache_misses": s.cache_misses,
                "evictions": s.evictions,
            }
        return snap

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._app_id = state.get("app_id", "default")

        for name, data in state.get("knowledge_bases", {}).items():
            kb = _KBMeta(
                name=name, description=data.get("description", ""),
                embedding_model=data.get("embedding_model", ""),
                vector_dim=data.get("vector_dim", 384),
            )
            kb.created_at = data.get("created_at", time.time())
            kb.doc_count = data.get("doc_count", 0)
            kb.chunk_count = data.get("chunk_count", 0)
            kb.content_hashes = data.get("content_hashes", {})
            bm25_data = data.get("bm25")
            if bm25_data:
                kb.bm25 = BM25Index.from_dict(bm25_data)
            self._kbs[name] = kb

        if self._backend:
            await self._backend.restore_state(state.get("backend", {}))

        if self._indexing:
            self._indexing.restore_state(state.get("indexing", {}))

        if self._cache:
            cs = state.get("cache_stats", {})
            if cs:
                self._cache.stats.total_queries = cs.get("total_queries", 0)
                self._cache.stats.cache_hits = cs.get("cache_hits", 0)
                self._cache.stats.cache_misses = cs.get("cache_misses", 0)
                self._cache.stats.evictions = cs.get("evictions", 0)

    # ── Manifest ──────────────────────────────────────────────────────

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        return []

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "Advanced RAG - hybrid search, re-ranking, citations, multi-source.",
            "author": "Digitorn Team",
            "tags": ["rag", "search", "embeddings", "vector", "retrieval", "citations"],
            "supported_constraints": self.CONSTRAINTS,
        })
