"""Vector module — RAG-native vector collections for user documents.

Agents create collections, embed documents, and perform semantic or hybrid
search. Shares the FastEmbed model singleton with context_builder.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field as PydanticField

from digitorn.modules.base import BaseModule, ActionResult, Platform
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ConstraintSpec, ModuleManifest

from .chunking import chunk_text
from .params import (
    AddDirectoryParams,
    AddFileParams,
    AddParams,
    CollectionStatsParams,
    CountParams,
    CreateCollectionParams,
    DeleteCollectionParams,
    DeleteDocsParams,
    GetParams,
    HybridSearchParams,
    ListCollectionsParams,
    SearchMultiParams,
    SearchParams,
    UpdateMetadataParams,
)

logger = logging.getLogger(__name__)

# Qdrant + FastEmbed — TRULY lazy imports.
# qdrant_client is ~4s to import (loads onnxruntime/numpy transitively).
# We defer the import until the module is actually used (first call to
# create_collection, search, etc.). The import is cached after the first call.
QdrantClient: Any = None
models: Any = None
_HAS_QDRANT: bool | None = None  # None = not checked yet


def _ensure_qdrant() -> bool:
    """Lazily import qdrant_client on first use. Cached after first call.

    Returns True if qdrant_client is available, False otherwise.
    """
    global QdrantClient, models, _HAS_QDRANT
    if _HAS_QDRANT is not None:
        return _HAS_QDRANT
    try:
        from qdrant_client import QdrantClient as _QC, models as _models
        QdrantClient = _QC
        models = _models
        _HAS_QDRANT = True
    except ImportError:
        _HAS_QDRANT = False
    return _HAS_QDRANT


_VECTOR_DIM = 384


# ---------------------------------------------------------------------------
# Config model — compile-time validation via CONFIG_MODEL
# ---------------------------------------------------------------------------

class VectorConfig(BaseModel):
    """Pydantic config for the vector module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = PydanticField(
        default="",
        description=(
            "Auto-injected by the daemon at module init time. "
            "Do NOT set manually in YAML — the daemon resolves it from "
            "the app's workspace/workspace_mode config."
        ),
    )
    default_chunk_size: int = PydanticField(500, ge=50, le=10000)
    default_overlap: int = PydanticField(50, ge=0, le=500)
    persistence_dir: str | None = None


# ---------------------------------------------------------------------------
# Internal collection metadata
# ---------------------------------------------------------------------------

class _CollectionMeta:
    """Internal metadata for a collection."""

    __slots__ = ("name", "description", "doc_count", "created_at", "id_map", "content_hashes")

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self.doc_count = 0
        self.created_at = time.time()
        self.id_map: dict[str, int] = {}  # doc_id -> qdrant point_id
        self.content_hashes: dict[str, str] = {}  # source_key -> md5 hash (dedup)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "doc_count": self.doc_count,
            "created_at": self.created_at,
        }


class VectorModule(BaseModule):
    MODULE_ID = "vector"
    VERSION = "1.1.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    CONFIG_MODEL = VectorConfig

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        return []  # Actions are self-descriptive via schema

    CONSTRAINTS = [
        ConstraintSpec(
            name="paths",
            type="string_list",
            description="Allowed path prefixes for file indexing.",
            scope="module",
            applies_to=["add_file", "add_directory"],
        ),
        ConstraintSpec(
            name="max_documents",
            type="integer",
            description="Maximum total documents across all collections.",
            scope="module",
            default=50000,
        ),
        ConstraintSpec(
            name="allowed_collections",
            type="string_list",
            description="Restrict collection names the agent can create/use.",
            scope="module",
        ),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client: QdrantClient | None = None
        self._collections: dict[str, _CollectionMeta] = {}
        self._app_id: str = "default"
        self._default_chunk_size: int = 500
        self._default_overlap: int = 50
        self._persistence_dir: str | None = None
        self._next_point_id: int = 0

    async def on_start(self) -> None:
        # Trigger lazy import of qdrant_client (4s on first call, cached after)
        if not _ensure_qdrant():
            logger.warning("vector module requires qdrant-client and fastembed packages")
            return

        self._app_id = getattr(self, "_app_id_override", "default")

        # Config is auto-validated via CONFIG_MODEL; read typed fields
        cfg = self._config
        if isinstance(cfg, VectorConfig):
            self._default_chunk_size = cfg.default_chunk_size
            self._default_overlap = cfg.default_overlap
            self._persistence_dir = cfg.persistence_dir
        elif isinstance(cfg, dict):
            self._default_chunk_size = cfg.get("default_chunk_size", 500)
            self._default_overlap = cfg.get("default_overlap", 50)
            self._persistence_dir = cfg.get("persistence_dir")

        if self._persistence_dir:
            self._client = QdrantClient(path=self._persistence_dir)
        else:
            self._client = QdrantClient(":memory:")
        logger.info("vector_started: app=%s persistent=%s", self._app_id, bool(self._persistence_dir))

    async def on_stop(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
        self._collections.clear()

    # ------------------------------------------------------------------
    # Constraint helpers (same pattern as filesystem module)
    # ------------------------------------------------------------------

    def _constraints(self) -> dict[str, Any]:
        """Return active constraints from the execution context."""
        ctx = getattr(self, "_context", None)
        if ctx is not None and ctx.constraints:
            return ctx.constraints
        return {}

    def _check_path(self, path: Path) -> str | None:
        """Return an error if *path* is outside allowed paths, else None."""
        allowed = self._constraints().get("paths")
        if not allowed:
            return None
        resolved = str(path.resolve())
        for prefix in allowed:
            if resolved.startswith(str(Path(prefix).resolve())):
                return None
        return f"Path '{path}' is outside allowed paths: {allowed}"

    def _check_collection(self, name: str) -> str | None:
        """Return an error if *name* is not in allowed collections, else None."""
        allowed = self._constraints().get("allowed_collections")
        if not allowed:
            return None
        if name not in allowed:
            return f"Collection '{name}' not in allowed list: {allowed}"
        return None

    def _check_doc_limit(self, adding: int = 0) -> str | None:
        """Return an error if adding docs would exceed max_documents, else None."""
        limit = self._constraints().get("max_documents")
        if not limit:
            return None
        total = sum(m.doc_count for m in self._collections.values()) + adding
        if total > limit:
            return f"Document limit exceeded: {total} > {limit} (max_documents constraint)"
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_embedder(self) -> Any:
        """Get the shared FastEmbed model from context_builder."""
        try:
            from digitorn.modules.context_builder.embeddings import _get_model
            return _get_model()
        except Exception:
            return None

    def _collection_name(self, name: str) -> str:
        return f"user_{self._app_id}_{name}"

    def _embed(self, texts: list[str]) -> list[list[float]]:
        model = self._get_embedder()
        if model is None:
            raise RuntimeError("FastEmbed model not available. Install fastembed package.")
        embeddings = list(model.embed(texts))
        return [e.tolist() for e in embeddings]

    def _alloc_point_id(self) -> int:
        pid = self._next_point_id
        self._next_point_id += 1
        return pid

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @action(
        description="Create a new vector collection for storing and searching embedded documents.",
        params_model=CreateCollectionParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        aliases=["creer_collection", "new_collection"],
        tags=["vector", "admin"],
        cli_label='Create collection',
        cli_param='name',
    )
    async def create_collection(self, params: CreateCollectionParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        if err := self._check_collection(params.name):
            return ActionResult(success=False, error=err)
        coll_name = self._collection_name(params.name)
        if params.name in self._collections:
            return ActionResult(success=True, data={"collection": params.name, "already_exists": True})
        self._client.create_collection(
            collection_name=coll_name,
            vectors_config=models.VectorParams(size=_VECTOR_DIM, distance=models.Distance.COSINE),
        )
        self._collections[params.name] = _CollectionMeta(name=params.name, description=params.description)
        return ActionResult(success=True, data={"collection": params.name, "created": True})

    @action(
        description="Delete a vector collection and all its documents permanently.",
        params_model=DeleteCollectionParams,
        risk_level="high",
        side_effects=["state_mutation"],
        irreversible=True,
        aliases=["supprimer_collection"],
        tags=["vector", "admin"],
        cli_label='Delete collection',
        cli_param='name',
    )
    async def delete_collection(self, params: DeleteCollectionParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        coll_name = self._collection_name(params.name)
        try:
            self._client.delete_collection(coll_name)
        except Exception:
            pass
        self._collections.pop(params.name, None)
        return ActionResult(success=True, data={"collection": params.name, "deleted": True})

    @action(
        description="List all vector collections with their document counts.",
        params_model=ListCollectionsParams,
        risk_level="low",
        aliases=["lister_collections"],
        tags=["vector", "info"],
        cli_label='Collections',
    )
    async def list_collections(self, params: ListCollectionsParams) -> ActionResult:
        data = [m.to_dict() for m in self._collections.values()]
        return ActionResult(success=True, data={"collections": data, "count": len(data)})

    @action(
        description="Add text documents to a collection — embeds and indexes them for semantic search.",
        params_model=AddParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        aliases=["ajouter", "indexer", "embed", "insert"],
        tags=["vector", "write"],
        cli_label='Index',
        cli_param='collection',
    )
    async def add(self, params: AddParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        if err := self._check_collection(params.collection):
            return ActionResult(success=False, error=err)
        meta = self._collections.get(params.collection)
        if meta is None:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found. Create it first.")

        coll_name = self._collection_name(params.collection)
        if err := self._check_doc_limit(len(params.documents)):
            return ActionResult(success=False, error=err)

        embeddings = self._embed(params.documents)

        points = []
        ids_used = []
        for i, (doc, emb) in enumerate(zip(params.documents, embeddings)):
            doc_id = params.ids[i] if params.ids and i < len(params.ids) else hashlib.md5(doc.encode()).hexdigest()[:16]
            point_id = self._alloc_point_id()
            meta.id_map[doc_id] = point_id
            payload: dict[str, Any] = {
                "text": doc,
                "doc_id": doc_id,
                "source": "manual",
                "added_at": time.time(),
            }
            if params.metadata and i < len(params.metadata):
                payload["custom_metadata"] = params.metadata[i]
            points.append(models.PointStruct(id=point_id, vector=emb, payload=payload))
            ids_used.append(doc_id)

        self._client.upsert(collection_name=coll_name, points=points)
        meta.doc_count += len(points)
        return ActionResult(success=True, data={
            "collection": params.collection,
            "added": len(points),
            "ids": ids_used,
        })

    @action(
        description="Read a file, split it into chunks, embed each chunk, and add to a collection.",
        params_model=AddFileParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        aliases=["indexer_fichier", "embed_file"],
        tags=["vector", "write"],
        cli_label='Index file',
        cli_param='path',
    )
    async def add_file(self, params: AddFileParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        if err := self._check_collection(params.collection):
            return ActionResult(success=False, error=err)
        meta = self._collections.get(params.collection)
        if meta is None:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found.")

        path = Path(params.path).resolve()
        if err := self._check_path(path):
            return ActionResult(success=False, error=err)
        if not path.exists():
            return ActionResult(success=False, error=f"File not found: {params.path}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return ActionResult(success=False, error=f"Cannot read file: {exc}")

        # Dedup: skip if content unchanged since last indexing
        content_hash = hashlib.md5(text.encode()).hexdigest()
        source_key = str(path)
        if meta.content_hashes.get(source_key) == content_hash:
            return ActionResult(success=True, data={
                "collection": params.collection,
                "file": source_key,
                "skipped": True,
                "reason": "content unchanged",
            })

        # Remove old points for this source before re-indexing
        coll_name = self._collection_name(params.collection)
        if source_key in meta.content_hashes:
            try:
                self._client.delete(
                    collection_name=coll_name,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[models.FieldCondition(
                                key="source", match=models.MatchValue(value=source_key),
                            )]
                        )
                    ),
                )
            except Exception:
                pass

        chunks = chunk_text(text, strategy=params.chunk_strategy, size=params.chunk_size, overlap=params.overlap)
        if not chunks:
            meta.content_hashes[source_key] = content_hash
            return ActionResult(success=True, data={"collection": params.collection, "added": 0, "reason": "No text to index."})

        if err := self._check_doc_limit(len(chunks)):
            return ActionResult(success=False, error=err)

        texts = [c.text for c in chunks]
        embeddings = self._embed(texts)

        points = []
        for chunk, emb in zip(chunks, embeddings):
            point_id = self._alloc_point_id()
            doc_id = hashlib.md5(chunk.text.encode()).hexdigest()[:16]
            meta.id_map[doc_id] = point_id
            payload: dict[str, Any] = {
                "text": chunk.text,
                "doc_id": doc_id,
                "source": source_key,
                "chunk_index": chunk.index,
                "start_char": chunk.start_char,
                "end_char": chunk.end_char,
                "added_at": time.time(),
            }
            if params.metadata:
                payload["custom_metadata"] = params.metadata
            points.append(models.PointStruct(id=point_id, vector=emb, payload=payload))

        self._client.upsert(collection_name=coll_name, points=points)
        meta.doc_count += len(points)
        meta.content_hashes[source_key] = content_hash
        return ActionResult(success=True, data={
            "collection": params.collection,
            "file": source_key,
            "chunks": len(chunks),
            "added": len(points),
            "strategy": params.chunk_strategy,
        })

    @action(
        description="Semantic search — find documents similar to a natural language query.",
        params_model=SearchParams,
        risk_level="low",
        aliases=["rechercher", "chercher", "query", "find_similar"],
        tags=["vector", "search"],
        cli_label='Search',
        cli_param='query',
        cli_show_output=True,
        cli_output_lines=5,
    )
    async def search(self, params: SearchParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        if params.collection not in self._collections:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found.")

        coll_name = self._collection_name(params.collection)
        query_emb = self._embed([params.query])[0]

        query_filter = None
        if params.filter:
            query_filter = models.Filter(**params.filter)

        results = self._client.query_points(
            collection_name=coll_name,
            query=query_emb,
            limit=params.top_k,
            score_threshold=params.min_score,
            query_filter=query_filter,
        ).points

        hits = []
        for r in results:
            hit = {
                "text": r.payload.get("text", ""),
                "doc_id": r.payload.get("doc_id", ""),
                "score": round(r.score, 4),
                "source": r.payload.get("source", ""),
            }
            if "custom_metadata" in r.payload:
                hit["metadata"] = r.payload["custom_metadata"]
            hits.append(hit)

        return ActionResult(success=True, data={
            "collection": params.collection,
            "query": params.query,
            "results": hits,
            "count": len(hits),
        })

    @action(
        description="Hybrid search combining semantic similarity with keyword matching for better recall.",
        params_model=HybridSearchParams,
        risk_level="low",
        aliases=["recherche_hybride"],
        tags=["vector", "search"],
        cli_label='Hybrid search',
        cli_param='query',
        cli_show_output=True,
        cli_output_lines=5,
    )
    async def hybrid_search(self, params: HybridSearchParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        if params.collection not in self._collections:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found.")

        coll_name = self._collection_name(params.collection)
        query_emb = self._embed([params.query])[0]

        # Semantic search — get more than top_k for re-ranking
        sem_results = self._client.query_points(
            collection_name=coll_name,
            query=query_emb,
            limit=params.top_k * 3,
            score_threshold=0.1,
        ).points

        # Keyword scoring — use scoring.tokenize for accent normalization + stop words
        try:
            from digitorn.modules.context_builder.scoring import tokenize as _tokenize
        except ImportError:
            _tokenize = lambda t: set(re.findall(r'\w+', t.lower()))  # noqa: E731
        query_tokens = set(_tokenize(params.query))
        scored: list[tuple[float, dict[str, Any]]] = []
        for r in sem_results:
            text = r.payload.get("text", "")
            text_tokens = set(_tokenize(text))
            overlap = len(query_tokens & text_tokens)
            keyword_score = overlap / max(len(query_tokens), 1)
            combined = params.semantic_weight * r.score + params.keyword_weight * keyword_score
            hit = {
                "text": text,
                "doc_id": r.payload.get("doc_id", ""),
                "score": round(combined, 4),
                "semantic_score": round(r.score, 4),
                "keyword_score": round(keyword_score, 4),
                "source": r.payload.get("source", ""),
            }
            if "custom_metadata" in r.payload:
                hit["metadata"] = r.payload["custom_metadata"]
            scored.append((combined, hit))

        scored.sort(key=lambda x: x[0], reverse=True)
        hits = [h for _, h in scored[:params.top_k]]

        return ActionResult(success=True, data={
            "collection": params.collection,
            "query": params.query,
            "results": hits,
            "count": len(hits),
        })

    @action(
        description="Retrieve specific documents by their IDs.",
        params_model=GetParams,
        risk_level="low",
        aliases=["obtenir_documents", "retrieve"],
        tags=["vector", "read"],
        cli_label='Get',
        cli_param='collection',
    )
    async def get(self, params: GetParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        meta = self._collections.get(params.collection)
        if meta is None:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found.")

        coll_name = self._collection_name(params.collection)
        point_ids = [meta.id_map[did] for did in params.ids if did in meta.id_map]
        if not point_ids:
            return ActionResult(success=True, data={"documents": [], "count": 0})

        points = self._client.retrieve(collection_name=coll_name, ids=point_ids, with_payload=True)
        docs = []
        for p in points:
            docs.append({
                "doc_id": p.payload.get("doc_id", ""),
                "text": p.payload.get("text", ""),
                "source": p.payload.get("source", ""),
                "metadata": p.payload.get("custom_metadata"),
            })
        return ActionResult(success=True, data={"documents": docs, "count": len(docs)})

    @action(
        description="Delete documents from a collection by IDs.",
        params_model=DeleteDocsParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        aliases=["supprimer_documents", "remove"],
        tags=["vector", "write"],
        cli_label='Delete',
        cli_param='collection',
    )
    async def delete(self, params: DeleteDocsParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        meta = self._collections.get(params.collection)
        if meta is None:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found.")

        coll_name = self._collection_name(params.collection)
        count = 0

        if params.ids:
            point_ids = [meta.id_map.pop(did) for did in params.ids if did in meta.id_map]
            if point_ids:
                self._client.delete(collection_name=coll_name, points_selector=models.PointIdsList(points=point_ids))
                count = len(point_ids)
                meta.doc_count = max(0, meta.doc_count - count)

        if params.filter:
            self._client.delete(
                collection_name=coll_name,
                points_selector=models.FilterSelector(filter=models.Filter(**params.filter)),
            )

        return ActionResult(success=True, data={"collection": params.collection, "deleted": count})

    @action(
        description="Update metadata for existing documents.",
        params_model=UpdateMetadataParams,
        risk_level="low",
        side_effects=["state_mutation"],
        aliases=["modifier_metadata"],
        tags=["vector", "write"],
        cli_label='Update meta',
        cli_param='collection',
    )
    async def update_metadata(self, params: UpdateMetadataParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        meta = self._collections.get(params.collection)
        if meta is None:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found.")

        coll_name = self._collection_name(params.collection)
        point_ids = [meta.id_map[did] for did in params.ids if did in meta.id_map]
        if not point_ids:
            return ActionResult(success=True, data={"updated": 0})

        self._client.set_payload(
            collection_name=coll_name,
            payload={"custom_metadata": params.metadata},
            points=point_ids,
        )
        return ActionResult(success=True, data={"updated": len(point_ids)})

    @action(
        description="Count documents in a collection.",
        params_model=CountParams,
        risk_level="low",
        aliases=["compter", "count_docs"],
        tags=["vector", "info"],
        cli_label='Count',
        cli_param='collection',
    )
    async def count(self, params: CountParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        if params.collection not in self._collections:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found.")

        coll_name = self._collection_name(params.collection)
        info = self._client.get_collection(coll_name)
        count = info.points_count
        return ActionResult(success=True, data={"collection": params.collection, "count": count})

    @action(
        description="Get detailed statistics for a collection: document count, vector dimensions, storage info.",
        params_model=CollectionStatsParams,
        risk_level="low",
        aliases=["statistiques_collection", "info_collection"],
        tags=["vector", "info"],
        cli_label='Stats',
        cli_param='collection',
    )
    async def collection_stats(self, params: CollectionStatsParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        meta = self._collections.get(params.collection)
        if meta is None:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found.")

        coll_name = self._collection_name(params.collection)
        try:
            info = self._client.get_collection(coll_name)
            return ActionResult(success=True, data={
                "collection": params.collection,
                "description": meta.description,
                "points_count": info.points_count,
                "vectors_count": info.vectors_count,
                "status": str(info.status),
                "vector_size": _VECTOR_DIM,
                "created_at": meta.created_at,
            })
        except Exception as exc:
            return ActionResult(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # New actions: add_directory, search_multi
    # ------------------------------------------------------------------

    @action(
        description="Index all files in a directory — walks the tree, chunks each file, embeds, and stores. Skips unchanged files (dedup).",
        params_model=AddDirectoryParams,
        risk_level="medium",
        side_effects=["state_mutation"],
        aliases=["indexer_repertoire", "embed_directory", "index_folder"],
        tags=["vector", "write", "batch"],
        cli_label='Index dir',
        cli_param='path',
    )
    async def add_directory(self, params: AddDirectoryParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")
        if err := self._check_collection(params.collection):
            return ActionResult(success=False, error=err)
        meta = self._collections.get(params.collection)
        if meta is None:
            return ActionResult(success=False, error=f"Collection '{params.collection}' not found.")

        dir_path = Path(params.path).resolve()
        if err := self._check_path(dir_path):
            return ActionResult(success=False, error=err)
        if not dir_path.is_dir():
            return ActionResult(success=False, error=f"Not a directory: {params.path}")

        ext_set = set(params.extensions)
        glob_method = dir_path.rglob if params.recursive else dir_path.glob
        candidates = sorted(
            (f for f in glob_method("*")
             if f.is_file()
             and f.suffix in ext_set
             and not f.name.startswith(".")),
            key=lambda f: f.name,
        )

        coll_name = self._collection_name(params.collection)
        files_indexed = 0
        files_skipped = 0
        total_chunks = 0
        errors: list[str] = []

        for fpath in candidates[:params.max_files]:
            if err := self._check_path(fpath):
                errors.append(f"{fpath.name}: {err}")
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                errors.append(f"{fpath.name}: {exc}")
                continue

            source_key = str(fpath)
            content_hash = hashlib.md5(text.encode()).hexdigest()
            if meta.content_hashes.get(source_key) == content_hash:
                files_skipped += 1
                continue

            # Remove old points for this source
            if source_key in meta.content_hashes:
                try:
                    self._client.delete(
                        collection_name=coll_name,
                        points_selector=models.FilterSelector(
                            filter=models.Filter(
                                must=[models.FieldCondition(
                                    key="source", match=models.MatchValue(value=source_key),
                                )]
                            )
                        ),
                    )
                except Exception:
                    pass

            chunks = chunk_text(text, strategy=params.chunk_strategy, size=params.chunk_size, overlap=params.overlap)
            if not chunks:
                meta.content_hashes[source_key] = content_hash
                files_skipped += 1
                continue

            texts = [c.text for c in chunks]
            embeddings = self._embed(texts)

            points = []
            for chunk, emb in zip(chunks, embeddings):
                point_id = self._alloc_point_id()
                doc_id = hashlib.md5(chunk.text.encode()).hexdigest()[:16]
                meta.id_map[doc_id] = point_id
                payload: dict[str, Any] = {
                    "text": chunk.text,
                    "doc_id": doc_id,
                    "source": source_key,
                    "chunk_index": chunk.index,
                    "start_char": chunk.start_char,
                    "end_char": chunk.end_char,
                    "added_at": time.time(),
                }
                if params.metadata:
                    payload["custom_metadata"] = params.metadata
                points.append(models.PointStruct(id=point_id, vector=emb, payload=payload))

            self._client.upsert(collection_name=coll_name, points=points)
            meta.doc_count += len(points)
            meta.content_hashes[source_key] = content_hash
            total_chunks += len(chunks)
            files_indexed += 1

        return ActionResult(success=True, data={
            "collection": params.collection,
            "directory": str(dir_path),
            "files_indexed": files_indexed,
            "files_skipped": files_skipped,
            "total_chunks": total_chunks,
            "errors": errors[:20] if errors else [],
        })

    @action(
        description="Search across multiple collections and merge results by score.",
        params_model=SearchMultiParams,
        risk_level="low",
        aliases=["recherche_multiple", "multi_search", "cross_search"],
        tags=["vector", "search"],
        cli_label='Multi search',
        cli_param='query',
        cli_show_output=True,
        cli_output_lines=5,
    )
    async def search_multi(self, params: SearchMultiParams) -> ActionResult:
        if not self._client:
            return ActionResult(success=False, error="Vector backend not initialized.")

        query_emb = self._embed([params.query])[0]
        all_hits: list[tuple[float, dict[str, Any]]] = []

        for coll in params.collections:
            if coll not in self._collections:
                continue
            coll_name = self._collection_name(coll)
            try:
                results = self._client.query_points(
                    collection_name=coll_name,
                    query=query_emb,
                    limit=params.top_k,
                    score_threshold=params.min_score,
                ).points
            except Exception:
                continue

            for r in results:
                hit = {
                    "text": r.payload.get("text", ""),
                    "doc_id": r.payload.get("doc_id", ""),
                    "score": round(r.score, 4),
                    "source": r.payload.get("source", ""),
                    "collection": coll,
                }
                if "custom_metadata" in r.payload:
                    hit["metadata"] = r.payload["custom_metadata"]
                all_hits.append((r.score, hit))

        all_hits.sort(key=lambda x: x[0], reverse=True)
        hits = [h for _, h in all_hits[:params.top_k]]

        return ActionResult(success=True, data={
            "query": params.query,
            "collections_searched": params.collections,
            "results": hits,
            "count": len(hits),
        })

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def state_snapshot(self) -> dict[str, Any]:
        collections = {}
        for n, m in self._collections.items():
            d = m.to_dict()
            d["content_hashes"] = dict(m.content_hashes)
            collections[n] = d
        return {
            "app_id": self._app_id,
            "collections": collections,
            "next_point_id": self._next_point_id,
        }

    async def restore_state(self, state: dict[str, Any]) -> None:
        self._app_id = state.get("app_id", "default")
        self._next_point_id = state.get("next_point_id", 0)
        for name, data in state.get("collections", {}).items():
            meta = _CollectionMeta(name=name, description=data.get("description", ""))
            meta.doc_count = data.get("doc_count", 0)
            meta.created_at = data.get("created_at", time.time())
            meta.content_hashes = data.get("content_hashes", {})
            self._collections[name] = meta

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": "RAG-native vector collections — create, embed, and search over user documents.",
            "author": "Digitorn Team",
            "tags": ["core", "vector", "rag", "embeddings", "search"],
            "supported_constraints": self.CONSTRAINTS,
        })
