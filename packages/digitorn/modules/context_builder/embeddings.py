"""Semantic search engine - FastEmbed + Qdrant embedded."""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

# lazy imports - the transitive ML deps add ~5s to module load.
TextEmbedding: Any = None
QdrantClient: Any = None
models: Any = None
_HAS_EMBEDDINGS: bool | None = None

def _ensure_embeddings() -> bool:
    global TextEmbedding, QdrantClient, models, _HAS_EMBEDDINGS
    if _HAS_EMBEDDINGS is not None:
        return _HAS_EMBEDDINGS
    try:
        from fastembed import TextEmbedding as _TE
        from qdrant_client import QdrantClient as _QC, models as _models
        TextEmbedding = _TE
        QdrantClient = _QC
        models = _models
        _HAS_EMBEDDINGS = True
    except ImportError:
        _HAS_EMBEDDINGS = False
    return _HAS_EMBEDDINGS

if TYPE_CHECKING:
    from digitorn.modules.context_builder.types import ToolIndex
    from fastembed import TextEmbedding as _TextEmbeddingType  # noqa: F401
    from qdrant_client import QdrantClient as _QdrantClientType  # noqa: F401

logger = logging.getLogger(__name__)

def _load_discovery_config():
    try:
        from digitorn.core.config import get_settings
        cfg = get_settings().discovery
        return cfg.embedding_model, cfg.embedding_dim, cfg.models_cache_dir
    except Exception:
        return "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384, ""

_MODEL_ID, _VECTOR_DIM, _CACHE_DIR_OVERRIDE = _load_discovery_config()

_COLLECTION = "tools"

_model: TextEmbedding | None = None

def _get_model(cache_dir: str | Path | None = None):
    if not _ensure_embeddings():
        return None
    global _model  # noqa: PLW0603
    if _model is not None:
        return _model

    if cache_dir is None:
        if _CACHE_DIR_OVERRIDE:
            cache_dir = _CACHE_DIR_OVERRIDE
        else:
            from digitorn.core.paths import models_dir
            cache_dir = models_dir()

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)

    logger.info(
        "Loading embedding model %s (cache: %s) …",
        _MODEL_ID, cache_path,
    )

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*now uses mean pooling.*")
        _model = TextEmbedding(_MODEL_ID, cache_dir=str(cache_path))

    logger.info("Embedding model ready.")
    return _model

class SemanticIndex:
    """HNSW-backed semantic index over tool embeddings."""

    def __init__(
        self,
        client: QdrantClient,
        fqns: list[str],
        model: TextEmbedding,
    ) -> None:
        self._client = client
        self._fqns = fqns
        self._model = model

    def search(
        self, query: str, top_k: int = 5, min_score: float = 0.35,
    ) -> list[tuple[str, float]]:
        """Semantic search over tool embeddings via HNSW. Returns descending-score list."""
        query_vec = list(self._model.embed([query]))[0].tolist()

        hits = self._client.query_points(
            collection_name=_COLLECTION,
            query=query_vec,
            limit=top_k,
            score_threshold=min_score,
        ).points

        results = [
            (self._fqns[hit.id], float(hit.score))
            for hit in hits
        ]
        results.sort(key=lambda r: r[1], reverse=True)
        return results

    @classmethod
    def build(
        cls,
        tool_index: ToolIndex,
        *,
        cache_dir: str | Path | None = None,
    ) -> SemanticIndex | None:
        """Build a semantic index from a ToolIndex (None if empty)."""
        if not tool_index.tools:
            return None

        model = _get_model(cache_dir)
        if model is None:
            logger.info("Semantic index disabled (fastembed not installed)")
            return None

        from digitorn.modules.context_builder.scoring import (
            expand_with_synonyms,
            tokenize_for_index,
        )

        fqns: list[str] = []
        texts: list[str] = []
        for fqn, tool in tool_index.tools.items():
            fqns.append(fqn)
            parts = [
                fqn.replace(".", " "),
                tool.description,
            ]
            if tool.tags:
                parts.append(" ".join(tool.tags))
            param_names = list(tool.params_schema.get("properties", {}).keys())
            if param_names:
                parts.append(" ".join(param_names))
            if tool.side_effects:
                parts.append(" ".join(tool.side_effects))
            if tool.aliases:
                parts.append(" ".join(tool.aliases))
            base_tokens = tokenize_for_index(
                f"{tool.action_name} {tool.description}"
            )
            synonyms = expand_with_synonyms(base_tokens)
            extra = [s for s in synonyms if s not in set(base_tokens)]
            if extra:
                parts.append(" ".join(extra))
            texts.append(" ".join(parts))

        logger.info(
            "Building semantic index: %d tools with model %s",
            len(fqns), _MODEL_ID,
        )

        embeddings = list(model.embed(texts))

        # refuse a partial index on silent truncation; caller will
        # fall back to keyword search.
        if len(embeddings) != len(texts):
            logger.error(
                "embedding_size_mismatch texts=%d embeddings=%d - "
                "semantic index disabled to prevent silent tool dropout. "
                "Keyword search will be used instead.",
                len(texts), len(embeddings),
            )
            return None

        client = QdrantClient(":memory:")
        client.create_collection(
            collection_name=_COLLECTION,
            vectors_config=models.VectorParams(
                size=_VECTOR_DIM,
                distance=models.Distance.COSINE,
            ),
        )

        points = [
            models.PointStruct(
                id=i,
                vector=emb.tolist(),
                payload={"fqn": fqn},
            )
            for i, (fqn, emb) in enumerate(zip(fqns, embeddings))
        ]
        client.upsert(collection_name=_COLLECTION, points=points)

        logger.info(
            "Semantic index ready: %d tools, HNSW indexed, %d dimensions",
            len(fqns), _VECTOR_DIM,
        )

        return cls(client=client, fqns=fqns, model=model)
