# Vector Module - Action Reference

## create_collection

Create a new vector collection for storing and searching documents.

**Parameters:**
- `name` (required): Collection name (alphanumeric + hyphens).
- `description`: Human-readable description of what this collection stores.

## delete_collection

Delete a vector collection and all its documents.

**Parameters:**
- `name` (required): Collection name to delete.

## list_collections

List all vector collections.

No parameters.

## add

Add documents to a collection (embeds and indexes them).

**Parameters:**
- `collection` (required): Target collection name.
- `documents` (required): List of text documents to embed and store.
- `ids`: Optional custom IDs for each document.
- `metadata`: Optional metadata per document.

## add_file

Read a file, chunk it, embed the chunks, and add to a collection.

**Parameters:**
- `collection` (required): Target collection name.
- `path` (required): File path to read and index.
- `chunk_strategy`: Chunking strategy: `fixed`, `sentence`, `paragraph`, or `recursive` (default: `recursive`).
- `chunk_size`: Target chunk size in characters (default: 500, range: 50-10000).
- `overlap`: Overlap between chunks in characters (default: 50, range: 0-500).
- `metadata`: Extra metadata to attach to all chunks.

## search

Semantic search over a collection - find documents similar to the query.

**Parameters:**
- `collection` (required): Collection to search.
- `query` (required): Natural language search query.
- `top_k`: Number of results to return (default: 5, max: 100).
- `min_score`: Minimum similarity score threshold (default: 0.3, range: 0.0-1.0).
- `filter`: Metadata filter (Qdrant payload filter format).

## hybrid_search

Hybrid search combining semantic similarity and keyword matching.

**Parameters:**
- `collection` (required): Collection to search.
- `query` (required): Search query.
- `top_k`: Number of results (default: 5, max: 100).
- `keyword_weight`: Weight for keyword matching, 0-1 (default: 0.3).
- `semantic_weight`: Weight for semantic similarity, 0-1 (default: 0.7).

## get

Retrieve documents by their IDs.

**Parameters:**
- `collection` (required): Collection name.
- `ids` (required): Document IDs to retrieve.

## delete

Delete documents from a collection by IDs or filter.

**Parameters:**
- `collection` (required): Collection name.
- `ids`: Document IDs to delete.
- `filter`: Metadata filter for bulk deletion.

## update_metadata

Update metadata for existing documents.

**Parameters:**
- `collection` (required): Collection name.
- `ids` (required): Document IDs to update.
- `metadata` (required): New metadata fields to set/merge.

## count

Count documents in a collection.

**Parameters:**
- `collection` (required): Collection name.

## collection_stats

Get detailed statistics for a collection.

**Parameters:**
- `collection` (required): Collection name.
