"""VectorBackend - abstract interface for vector databases."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

@dataclass(slots=True)
class CollectionInfo:
    """Metadata about a vector collection."""

    name: str
    dimension: int
    doc_count: int = 0
    description: str = ""
    embedding_model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(slots=True)
class Document:
    """A stored document with its vector and metadata."""

    doc_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(slots=True)
class SearchResult:
    """A single search result."""

    doc_id: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

class VectorBackend(ABC):
    """Abstract interface for vector database backends."""

    supports_sparse: bool = False
    supports_hybrid: bool = False
    supports_metadata_filter: bool = True

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the backend (connect, create client, etc.)."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources."""

    @abstractmethod
    async def create_collection(
        self, name: str, dimension: int, metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create a vector collection. No-op if already exists."""

    @abstractmethod
    async def delete_collection(self, name: str) -> bool:
        """Delete a collection. Returns True if it existed."""

    @abstractmethod
    async def list_collections(self) -> list[CollectionInfo]:
        """List all collections with metadata."""

    @abstractmethod
    async def collection_exists(self, name: str) -> bool:
        """Check if a collection exists."""

    @abstractmethod
    async def upsert(
        self,
        collection: str,
        ids: list[str],
        vectors: list[list[float]],
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
    ) -> int:
        """Insert or update documents. Returns count upserted."""

    @abstractmethod
    async def delete(self, collection: str, ids: list[str]) -> int:
        """Delete documents by ID. Returns count deleted."""

    @abstractmethod
    async def get(self, collection: str, ids: list[str]) -> list[Document]:
        """Retrieve documents by ID."""

    @abstractmethod
    async def count(self, collection: str) -> int:
        """Return the number of documents in a collection."""

    async def get_all(self, collection: str) -> list[dict[str, Any]]:
        """Retrieve all documents from a collection. Used for migration."""
        raise NotImplementedError(f"{type(self).__name__} does not implement get_all.")

    @abstractmethod
    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 10,
        min_score: float = 0.0,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Semantic (dense vector) search."""

    async def hybrid_search(
        self,
        collection: str,
        vector: list[float],
        text_query: str,
        top_k: int = 10,
        alpha: float = 0.7,
        filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        """Native hybrid search (dense + sparse/FTS)."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support native hybrid search. "
            "The RAG pipeline will use BM25 + RRF fusion instead."
        )

    def state_snapshot(self) -> dict[str, Any]:
        """Serialize backend state for persistence."""
        return {}

    async def restore_state(self, state: dict[str, Any]) -> None:
        """Restore backend from persisted state."""
