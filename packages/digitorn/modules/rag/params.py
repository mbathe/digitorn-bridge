"""Pydantic parameter models for RAG module actions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateKnowledgeBaseParams(BaseModel):
    """Create a new knowledge base (vector collection + BM25 index)."""

    name: str = Field(..., min_length=1, max_length=128, description="Knowledge base name.")
    description: str = Field("", max_length=512, description="Human-readable description.")
    embedding_model: str = Field(
        "",
        description="Override the default embedding model for this KB. Empty = use module default.",
    )


class DeleteKnowledgeBaseParams(BaseModel):
    """Delete a knowledge base and all its data."""

    name: str = Field(..., min_length=1, description="Knowledge base name to delete.")


class ListKnowledgeBasesParams(BaseModel):
    """List all knowledge bases."""
    pass


class KnowledgeBaseStatsParams(BaseModel):
    """Get detailed stats for a knowledge base."""

    name: str = Field(..., min_length=1, description="Knowledge base name.")


class IngestParams(BaseModel):
    """Ingest raw text documents into a knowledge base."""

    knowledge_base: str = Field(..., min_length=1, description="Target knowledge base name.")
    documents: list[str] = Field(..., min_length=1, description="Text documents to ingest.")
    ids: list[str] | None = Field(None, description="Optional document IDs (auto-generated if omitted).")
    metadata: list[dict[str, Any]] | None = Field(
        None, description="Optional per-document metadata.",
    )
    source_type: str = Field("manual", description="Source type for citations (manual, file, database, web).")
    source_id: str = Field("", description="Source identifier for citations.")


class IngestFileParams(BaseModel):
    """Ingest a file into a knowledge base with automatic format detection."""

    knowledge_base: str = Field(..., min_length=1, description="Target knowledge base name.")
    path: str = Field(..., min_length=1, description="File path to ingest.")
    chunk_strategy: Literal["fixed", "sentence", "paragraph", "recursive"] = Field(
        "", description="Chunking strategy. Empty = use module default.",
    )
    chunk_size: int = Field(0, ge=0, le=10000, description="Chunk size. 0 = use module default.")
    chunk_overlap: int = Field(-1, ge=-1, le=500, description="Chunk overlap. -1 = use module default.")
    metadata: dict[str, Any] | None = Field(None, description="Extra metadata attached to all chunks.")


class IngestDirectoryParams(BaseModel):
    """Ingest all matching files from a directory."""

    knowledge_base: str = Field(..., min_length=1, description="Target knowledge base name.")
    path: str = Field(..., min_length=1, description="Directory path.")
    extensions: list[str] = Field(
        default_factory=lambda: [".md", ".txt", ".pdf"],
        description="File extensions to include.",
    )
    recursive: bool = Field(True, description="Recurse into subdirectories.")
    max_files: int = Field(1000, ge=1, le=50000, description="Maximum files to process.")
    chunk_strategy: str = Field("", description="Chunking strategy override.")
    chunk_size: int = Field(0, ge=0, description="Chunk size override.")
    chunk_overlap: int = Field(-1, ge=-1, description="Chunk overlap override.")


class QueryParams(BaseModel):
    """Search a knowledge base with the configured pipeline."""

    knowledge_base: str = Field(..., min_length=1, description="Knowledge base to search.")
    query: str = Field(..., min_length=1, description="Search query.")
    top_k: int = Field(5, ge=1, le=100, description="Number of results to return.")
    min_score: float = Field(0.0, ge=0.0, le=1.0, description="Minimum relevance score filter.")
    strategy: Literal["hybrid", "semantic", "bm25", "adaptive", ""] = Field(
        "", description="Override retrieval strategy. Empty = use pipeline default.",
    )
    filter: dict[str, Any] | None = Field(None, description="Metadata filter (backend-specific).")


class ClearCacheParams(BaseModel):
    """Clear the semantic cache."""

    knowledge_base: str = Field("", description="Clear cache for a specific KB. Empty = clear all.")


class IngestDatabaseParams(BaseModel):
    knowledge_base: str = Field(..., min_length=1, description="Target knowledge base.")
    connection_id: str = Field(..., min_length=1, description="Database connection ID.")
    tables: dict[str, dict[str, Any]] = Field(
        ..., description="Table configs: {table_name: {columns, mode, template, max_rows}}.",
    )


class MultiQueryParams(BaseModel):
    knowledge_base: str = Field(..., min_length=1, description="Knowledge base to search.")
    query: str = Field(..., min_length=1, description="Search query.")
    top_k: int = Field(5, ge=1, le=100, description="Number of results to return.")
    num_variants: int = Field(3, ge=2, le=10, description="Number of query variants to generate.")
    min_score: float = Field(0.0, ge=0.0, le=1.0, description="Minimum relevance score filter.")
    filter: dict[str, Any] | None = Field(None, description="Metadata filter.")


class SQLQueryParams(BaseModel):
    query: str = Field(..., min_length=1, description="Natural language question.")
    connection_id: str = Field(..., min_length=1, description="Database connection ID.")
    knowledge_base: str = Field("", description="KB with schema info. Empty = auto-detect.")
    top_k: int = Field(5, ge=1, le=100, description="Max results.")


class MigrateEmbeddingsParams(BaseModel):
    knowledge_base: str = Field(..., min_length=1, description="Knowledge base to migrate.")
    target_model: str = Field(..., min_length=1, description="New embedding model (shortcut or ID).")


class ListModelsParams(BaseModel):
    """List available embedding models."""
    pass
