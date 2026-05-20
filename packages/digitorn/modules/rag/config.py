"""RAG module configuration - Pydantic models validated at compile time."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

class ChunkingConfig(BaseModel):
    strategy: Literal["fixed", "sentence", "paragraph", "recursive"] = Field(
        "recursive", description="Text splitting strategy.",
    )
    size: int = Field(500, ge=50, le=10000, description="Target chunk size in characters.")
    overlap: int = Field(50, ge=0, le=500, description="Overlap between adjacent chunks.")

class MultiQueryConfig(BaseModel):
    enabled: bool = False
    provider: str = Field("", description="LLM provider_id for query generation.")
    num_variants: int = Field(3, ge=2, le=10, description="Number of query variants.")

class PipelineConfig(BaseModel):
    retrieval: Literal["hybrid", "semantic", "bm25"] = Field(
        "hybrid", description="Default retrieval strategy.",
    )
    bm25_weight: float = Field(0.3, ge=0.0, le=1.0)
    semantic_weight: float = Field(0.7, ge=0.0, le=1.0)
    rerank_top_n: int = Field(20, ge=0, le=200, description="Candidates to re-rank. 0 = skip.")
    final_top_k: int = Field(5, ge=1, le=100)
    multi_query: MultiQueryConfig | None = None

class CacheConfig(BaseModel):
    enabled: bool = Field(True, description="Enable semantic caching of RAG responses.")
    backend: Literal["memory", "redis"] = "memory"
    similarity_threshold: float = Field(0.95, ge=0.80, le=1.0)
    ttl: int = Field(3600, ge=60, description="TTL in seconds.")
    max_entries: int = Field(10000, ge=100)
    redis_url: str = Field("", description="Redis URL when backend=redis.")

class CitationConfig(BaseModel):
    enabled: bool = Field(True, description="Inject source metadata into LLM context.")
    format: Literal["inline", "footnote", "structured"] = "inline"
    verify: bool = Field(False, description="Verify citations in LLM output post-generation.")

class ContextualRetrievalConfig(BaseModel):
    enabled: bool = False
    provider: str = Field("", description="LLM provider_id for context generation.")
    concurrency: int = Field(5, ge=1, le=20)
    prompt_template: str = Field(
        "", description="Custom template with {document} and {chunk} placeholders.",
    )

class CragConfig(BaseModel):
    enabled: bool = False
    provider: str = Field("", description="LLM provider_id for quality evaluation.")
    confidence_threshold: float = Field(0.5, ge=0.0, le=1.0)
    fallback: Literal["broader_query", "none"] = "broader_query"

class AdaptiveConfig(BaseModel):
    enabled: bool = False
    provider: str = Field("", description="LLM provider_id for query classification.")
    strategies: dict[str, PipelineConfig] = Field(default_factory=dict)

class Text2SQLConfig(BaseModel):
    enabled: bool = False
    provider: str = Field("", description="LLM provider_id for SQL generation.")
    example_cache: bool = Field(True, description="Cache validated (question, SQL) pairs.")

class TableConfig(BaseModel):
    columns: list[str] = Field(default_factory=list, description="Columns to index.")
    mode: Literal["schema_only", "embed_rows"] = "schema_only"
    sync: Literal["updated_at", "changelog", "notify", ""] = ""
    template: str = Field("", description="Row text template, e.g. '{name} ({dept}) - {bio}'.")
    max_rows: int = Field(50000, ge=1)

class DatabaseSyncConfig(BaseModel):
    strategy: Literal["updated_at", "changelog", "notify"] = "updated_at"
    interval: int = Field(30, ge=5, le=3600, description="Poll interval in seconds.")
    auto_create_triggers: bool = True
    prune_after_hours: int = Field(24, ge=1)

class DatabaseSourceConfig(BaseModel):
    type: Literal["database"] = "database"
    connection_id: str = Field(..., description="Reference to a database module connection.")
    sync: DatabaseSyncConfig = Field(default_factory=DatabaseSyncConfig)
    tables: dict[str, TableConfig] = Field(
        default_factory=dict, description="Explicit table configs. Unlisted tables are ignored.",
    )

class FileSourceConfig(BaseModel):
    type: Literal["file"] = "file"
    path: str = Field(..., description="Directory path (supports {{variables}}).")
    extensions: list[str] = Field(default_factory=lambda: [".md", ".txt", ".pdf"])
    watch: bool = False
    recursive: bool = True
    max_files: int = Field(1000, ge=1)

class AutoIndexConfig(BaseModel):
    on_start: bool = True
    schedule: str = Field("", description="Cron expression for periodic re-indexing.")

class BackendConfig(BaseModel):
    type: Literal["qdrant", "chroma", "lancedb", "pinecone", "pgvector", "elasticsearch"] = "qdrant"
    path: str = Field("", description="Persistent storage path. Empty = in-memory.")
    url: str = Field("", description="Remote server URL.")
    api_key: str = Field("", description="API key for cloud backends.")
    index_name: str = ""
    cloud: str = "aws"
    region: str = "us-east-1"
    dsn: str = Field("", description="PostgreSQL DSN for pgvector.")
    quantization: Literal["none", "int8", "binary"] = "none"

class CustomEmbeddingConfig(BaseModel):
    id: str = Field(..., description="HuggingFace model ID.")
    dimensions: int = Field(..., ge=1)
    pooling: Literal["mean", "cls"] = "mean"
    model_file: str = "onnx/model.onnx"

class RagConfig(BaseModel):
    """Root configuration - `rag: {}` for zero-config, or detailed setup."""

    model_config = {"extra": "forbid"}

    workspace: str = Field(
        default="",
        description=(
            "Auto-injected by the daemon at module init time. "
            "Do NOT set manually in YAML - the daemon resolves it from "
            "the app's workspace/workspace_mode config."
        ),
    )
    embedding_model: str | CustomEmbeddingConfig = Field(
        "minilm-l12",
        description=(
            "Shortcuts: minilm-l12, bge-m3, bge-small, nomic-v1.5, jina-v3. "
            "Or any FastEmbed model ID. Or {id, dimensions, pooling, model_file}."
        ),
    )
    reranker: bool | str = Field(
        False,
        description="true = default reranker (minilm-l6). Or a FastEmbed reranker model ID.",
    )

    backend: BackendConfig = Field(default_factory=BackendConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)

    sources: list[FileSourceConfig | DatabaseSourceConfig] = Field(default_factory=list)
    auto_index: AutoIndexConfig = Field(default_factory=AutoIndexConfig)

    cache: CacheConfig = Field(default_factory=CacheConfig)
    citations: CitationConfig = Field(default_factory=CitationConfig)
    contextual_retrieval: ContextualRetrievalConfig = Field(
        default_factory=ContextualRetrievalConfig,
    )

    text2sql: Text2SQLConfig = Field(default_factory=Text2SQLConfig)
    crag: CragConfig = Field(default_factory=CragConfig)
    adaptive: AdaptiveConfig = Field(default_factory=AdaptiveConfig)

    max_knowledge_bases: int = Field(50, ge=1)
    max_documents: int = Field(100000, ge=1)
    persistence_dir: str = ""
