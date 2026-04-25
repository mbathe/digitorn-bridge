"""EmbeddingManager — open model registry with auto-download.

Resolution: shortcut ("bge-m3") -> FastEmbed ID ("BAAI/bge-m3") -> custom config.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    fastembed_id: str
    dimensions: int
    description: str = ""


BUILTIN_MODELS: dict[str, ModelSpec] = {
    "minilm-l12": ModelSpec(
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", 384,
        "Fast multilingual, 50 langs, 220 MB",
    ),
    "bge-m3": ModelSpec("BAAI/bge-m3", 1024, "SOTA multilingual, 100+ langs, 2.3 GB"),
    "bge-small": ModelSpec("BAAI/bge-small-en-v1.5", 384, "Fast English, 67 MB"),
    "bge-large": ModelSpec("BAAI/bge-large-en-v1.5", 1024, "Large English, 1.2 GB"),
    "nomic-v1.5": ModelSpec("nomic-ai/nomic-embed-text-v1.5", 768, "Long-context EN, 8k tokens"),
    "jina-v3": ModelSpec("jinaai/jina-embeddings-v3", 1024, "Multilingual 90+ langs, 8k tokens"),
    "snowflake-xs": ModelSpec("snowflake/snowflake-arctic-embed-xs", 384, "Lightweight EN, 90 MB"),
}


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    fastembed_id: str
    dimensions: int
    is_custom: bool = False
    custom_pooling: str = "mean"
    custom_model_file: str = "onnx/model.onnx"


class EmbeddingManager:
    """Multi-model registry with lazy singleton loading."""

    def __init__(self, cache_dir: str | Path | None = None) -> None:
        if cache_dir is None:
            from digitorn.core.paths import models_dir
            cache_dir = models_dir()
        self._cache_dir = Path(cache_dir)
        self._loaded: dict[str, Any] = {}
        self._resolved: dict[str, ResolvedModel] = {}

    def resolve(self, model_spec: str | dict[str, Any] | Any) -> ResolvedModel:
        cache_key = str(model_spec)
        if cache_key in self._resolved:
            return self._resolved[cache_key]

        if isinstance(model_spec, str):
            resolved = self._resolve_string(model_spec)
        elif isinstance(model_spec, dict):
            resolved = self._resolve_dict(model_spec)
        elif hasattr(model_spec, "id") and hasattr(model_spec, "dimensions"):
            resolved = ResolvedModel(
                fastembed_id=model_spec.id,
                dimensions=model_spec.dimensions,
                is_custom=True,
                custom_pooling=getattr(model_spec, "pooling", "mean"),
                custom_model_file=getattr(model_spec, "model_file", "onnx/model.onnx"),
            )
        else:
            raise ValueError(f"Cannot resolve embedding model: {model_spec!r}")

        self._resolved[cache_key] = resolved
        return resolved

    def _resolve_string(self, spec: str) -> ResolvedModel:
        if spec in BUILTIN_MODELS:
            m = BUILTIN_MODELS[spec]
            return ResolvedModel(m.fastembed_id, m.dimensions)

        dims = self._lookup_fastembed_dims(spec)
        if dims is not None:
            return ResolvedModel(spec, dims)

        if ":" in spec:
            parts = spec.rsplit(":", 1)
            try:
                return ResolvedModel(parts[0], int(parts[1]), is_custom=True)
            except ValueError:
                pass

        raise ValueError(
            f"Unknown embedding model: {spec!r}. "
            f"Use a shortcut ({', '.join(BUILTIN_MODELS)}), "
            f"a FastEmbed model ID, or 'model_id:dimensions'."
        )

    def _resolve_dict(self, spec: dict[str, Any]) -> ResolvedModel:
        if "id" not in spec or "dimensions" not in spec:
            raise ValueError("Custom embedding config needs 'id' and 'dimensions'.")
        return ResolvedModel(
            fastembed_id=spec["id"],
            dimensions=spec["dimensions"],
            is_custom=True,
            custom_pooling=spec.get("pooling", "mean"),
            custom_model_file=spec.get("model_file", "onnx/model.onnx"),
        )

    @staticmethod
    def _lookup_fastembed_dims(model_id: str) -> int | None:
        try:
            from fastembed import TextEmbedding
            for info in TextEmbedding.list_supported_models():
                if info.get("model") == model_id:
                    return info.get("dim")
        except Exception:
            pass
        return None

    def get_model(self, resolved: ResolvedModel) -> Any:
        key = resolved.fastembed_id
        if key in self._loaded:
            return self._loaded[key]

        if resolved.fastembed_id == BUILTIN_MODELS["minilm-l12"].fastembed_id:
            try:
                from digitorn.modules.context_builder.embeddings import _get_model
                model = _get_model(cache_dir=self._cache_dir)
                if model is not None:
                    self._loaded[key] = model
                    return model
            except Exception:
                pass

        from fastembed import TextEmbedding

        if resolved.is_custom:
            from fastembed.common.model_description import ModelSource
            try:
                TextEmbedding.add_custom_model(
                    model=resolved.fastembed_id,
                    sources=ModelSource(hf=resolved.fastembed_id),
                    dim=resolved.dimensions,
                    model_file=resolved.custom_model_file,
                )
            except Exception:
                pass

        logger.info(
            "Loading embedding model %s (%d dims)",
            resolved.fastembed_id, resolved.dimensions,
        )
        model = TextEmbedding(resolved.fastembed_id, cache_dir=str(self._cache_dir))
        self._loaded[key] = model
        return model

    def embed(self, texts: list[str], resolved: ResolvedModel) -> list[list[float]]:
        model = self.get_model(resolved)
        return [e.tolist() if hasattr(e, "tolist") else list(e) for e in model.embed(texts)]

    def embed_single(self, text: str, resolved: ResolvedModel) -> list[float]:
        return self.embed([text], resolved)[0]

    @staticmethod
    def list_builtin() -> dict[str, dict[str, Any]]:
        return {
            name: {
                "fastembed_id": m.fastembed_id,
                "dimensions": m.dimensions,
                "description": m.description,
            }
            for name, m in BUILTIN_MODELS.items()
        }
