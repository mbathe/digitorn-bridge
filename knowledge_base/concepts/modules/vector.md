---
id: module-concept-vector
title: "vector module - overview"
type: module-concept
module: vector
isolation: shared
keywords: [vector, vector-module, create_collection, delete_collection, list_collections, add, add_file, search, hybrid_search, get, delete, update_metadata, count, collection_stats, add_directory, search_multi]
version: 1.1.0
---

# `vector` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.1.0`
- **Actions**: 14 visible, 0 internal

## Description (from class docstring)

Vector module - RAG-native vector collections for user documents.

Agents create collections, embed documents, and perform semantic or hybrid
search. Shares the FastEmbed model singleton with context_builder.

## Configuration

Set under `modules.vector.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon at module init time. Do NOT set manually in YAML - the daemon resolves it from the app's workspace/workspace_mode config. |
| `default_chunk_size` | int |  | `500` |  |
| `default_overlap` | int |  | `50` |  |
| `persistence_dir` | str \| None |  | `None` |  |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `create_collection` | `VectorCreateCollection` |  | medium | Create a new vector collection for storing and searching embedded documents. |
| `delete_collection` | `VectorDeleteCollection` |  | high | Delete a vector collection and all its documents permanently. |
| `list_collections` | `VectorListCollections` |  | low | List all vector collections with their document counts. |
| `add` | `VectorAdd` |  | medium | Add text documents to a collection - embeds and indexes them for semantic search. |
| `add_file` | `VectorAddFile` |  | medium | Read a file, split it into chunks, embed each chunk, and add to a collection. |
| `search` | `VectorSearch` |  | low | Semantic search - find documents similar to a natural language query. |
| `hybrid_search` | `VectorHybridSearch` |  | low | Hybrid search combining semantic similarity with keyword matching for better recall. |
| `get` | `VectorGet` |  | low | Retrieve specific documents by their IDs. |
| `delete` | `VectorDelete` |  | medium | Delete documents from a collection by IDs. |
| `update_metadata` | `VectorUpdateMetadata` |  | low | Update metadata for existing documents. |
| `count` | `VectorCount` |  | low | Count documents in a collection. |
| `collection_stats` | `VectorCollectionStats` |  | low | Get detailed statistics for a collection: document count, vector dimensions, storage info. |
| `add_directory` | `VectorAddDirectory` |  | medium | Index all files in a directory - walks the tree, chunks each file, embeds, and stores. Skips unchanged files (dedup). |
| `search_multi` | `VectorSearchMulti` |  | low | Search across multiple collections and merge results by score. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: vector
      actions: [create_collection, delete_collection, list_collections, add, add_file, search, hybrid_search, get, delete, update_metadata, count, collection_stats, add_directory, search_multi]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {vector: [create_collection, delete_collection, list_collections, add, add_file]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/vector-*.md`.
