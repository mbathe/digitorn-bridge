"""Vector module — input models for actions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateCollectionParams(BaseModel):
    """Create a new vector collection for storing and searching documents."""

    name: str = Field(..., description="Collection name (alphanumeric + hyphens).")
    description: str = Field("", description="Human-readable description of what this collection stores.")


class DeleteCollectionParams(BaseModel):
    """Delete a vector collection and all its documents."""

    name: str = Field(..., description="Collection name to delete.")


class ListCollectionsParams(BaseModel):
    """List all vector collections."""


class AddParams(BaseModel):
    """Add documents to a collection (embeds and indexes them)."""

    collection: str = Field(..., description="Target collection name.")
    documents: list[str] = Field(..., description="List of text documents to embed and store.", min_length=1)
    ids: list[str] | None = Field(None, description="Optional custom IDs for each document.")
    metadata: list[dict[str, Any]] | None = Field(None, description="Optional metadata per document.")


class AddFileParams(BaseModel):
    """Read a file, chunk it, embed the chunks, and add to a collection."""

    collection: str = Field(..., description="Target collection name.")
    path: str = Field(..., description="File path to read and index.")
    chunk_strategy: str = Field(
        "recursive",
        description="Chunking strategy: 'fixed', 'sentence', 'paragraph', or 'recursive'.",
    )
    chunk_size: int = Field(500, description="Target chunk size in characters.", ge=50, le=10000)
    overlap: int = Field(50, description="Overlap between chunks in characters.", ge=0, le=500)
    metadata: dict[str, Any] | None = Field(None, description="Extra metadata to attach to all chunks.")


class SearchParams(BaseModel):
    """Semantic search over a collection — find documents similar to the query."""

    collection: str = Field(..., description="Collection to search.")
    query: str = Field(..., description="Natural language search query.")
    top_k: int = Field(5, description="Number of results to return.", ge=1, le=100)
    min_score: float = Field(0.3, description="Minimum similarity score threshold.", ge=0.0, le=1.0)
    filter: dict[str, Any] | None = Field(None, description="Metadata filter (Qdrant payload filter format).")


class HybridSearchParams(BaseModel):
    """Hybrid search combining semantic similarity and keyword matching."""

    collection: str = Field(..., description="Collection to search.")
    query: str = Field(..., description="Search query.")
    top_k: int = Field(5, description="Number of results.", ge=1, le=100)
    keyword_weight: float = Field(0.3, description="Weight for keyword matching (0-1).", ge=0.0, le=1.0)
    semantic_weight: float = Field(0.7, description="Weight for semantic similarity (0-1).", ge=0.0, le=1.0)


class GetParams(BaseModel):
    """Retrieve documents by their IDs."""

    collection: str = Field(..., description="Collection name.")
    ids: list[str] = Field(..., description="Document IDs to retrieve.", min_length=1)


class DeleteDocsParams(BaseModel):
    """Delete documents from a collection by IDs or filter."""

    collection: str = Field(..., description="Collection name.")
    ids: list[str] | None = Field(None, description="Document IDs to delete.")
    filter: dict[str, Any] | None = Field(None, description="Metadata filter for bulk deletion.")


class UpdateMetadataParams(BaseModel):
    """Update metadata for existing documents."""

    collection: str = Field(..., description="Collection name.")
    ids: list[str] = Field(..., description="Document IDs to update.", min_length=1)
    metadata: dict[str, Any] = Field(..., description="New metadata fields to set/merge.")


class CountParams(BaseModel):
    """Count documents in a collection."""

    collection: str = Field(..., description="Collection name.")


class CollectionStatsParams(BaseModel):
    """Get detailed statistics for a collection."""

    collection: str = Field(..., description="Collection name.")


class AddDirectoryParams(BaseModel):
    """Read all files in a directory, chunk them, embed, and add to a collection."""

    collection: str = Field(..., description="Target collection name.")
    path: str = Field(..., description="Directory path to index.")
    extensions: list[str] = Field(
        default=[".txt", ".md", ".py", ".json", ".yaml", ".yml", ".rst", ".csv", ".log"],
        description="File extensions to include.",
    )
    chunk_strategy: str = Field("recursive", description="Chunking strategy: 'fixed', 'sentence', 'paragraph', or 'recursive'.")
    chunk_size: int = Field(500, description="Target chunk size in characters.", ge=50, le=10000)
    overlap: int = Field(50, description="Overlap between chunks in characters.", ge=0, le=500)
    recursive: bool = Field(True, description="Recurse into subdirectories.")
    max_files: int = Field(100, description="Maximum files to index.", ge=1, le=1000)
    metadata: dict[str, Any] | None = Field(None, description="Extra metadata to attach to all chunks.")


class SearchMultiParams(BaseModel):
    """Search across multiple collections and merge results by score."""

    collections: list[str] = Field(..., description="Collection names to search.", min_length=1)
    query: str = Field(..., description="Natural language search query.")
    top_k: int = Field(5, description="Total results to return (merged across collections).", ge=1, le=100)
    min_score: float = Field(0.3, description="Minimum similarity score threshold.", ge=0.0, le=1.0)


