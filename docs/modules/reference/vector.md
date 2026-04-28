---
id: vector
title: Vector Module
sidebar_label: vector
sidebar_position: 14
description: RAG-native vector collections - FastEmbed embeddings, Qdrant storage, 4 chunking strategies, hybrid search.
---

# vector

RAG-native vector module. Agents create collections, index documents with automatic chunking, and perform semantic or hybrid search. Shares the FastEmbed model with `context_builder` - zero extra memory.

| Property | Value |
|----------|-------|
| **Module ID** | `vector` |
| **Version** | `1.0.0` |
| **Platforms** | All |
| **Dependencies** | `fastembed`, `qdrant-client` |

---

## Design Philosophy

- **Shared model** - reuses `context_builder`'s FastEmbed singleton (no duplicate model loading)
- **Collection isolation** - user collections are `user_{app_id}_{name}`, separate from system `tools` collection
- **Automatic chunking** - 4 strategies from simple (fixed) to universal (recursive)
- **Hybrid search** - combine semantic similarity with keyword matching for better precision

---

## Actions (14)

### create_collection
Create a named vector collection. Parameters: `name`, `distance_metric`. **Risk: medium**

### delete_collection
Delete a collection and all its documents. Parameters: `name`. **Risk: high**

### list_collections
List all user collections with metadata. **Risk: low**

### collection_stats
Collection statistics: document count, dimensions, size. Parameters: `name`. **Risk: low**

### add
Add text documents with chunking and embedding. Parameters: `collection`, `documents` (list of `{text, source?, metadata?}`), `chunk_strategy`, `chunk_size`, `chunk_overlap`. **Risk: medium**

### add_file
Read a file, chunk, embed, and index all chunks. Parameters: `collection`, `file_path`, `chunk_strategy`, `chunk_size`, `chunk_overlap`, `metadata`. **Risk: medium**

### add_directory
Recursively ingest a directory - each file is chunked + embedded. Parameters: `collection`, `path`, `pattern` (glob), `chunk_strategy`, `chunk_size`, `chunk_overlap`, `metadata`. **Risk: medium**

### search
Semantic search using vector similarity. Parameters: `collection`, `query`, `limit`, `score_threshold`, `filter`. **Risk: low**

### search_multi
Fan-out semantic search across several collections in one call, merged + re-ranked. Parameters: `collections: list`, `query`, `limit`, `score_threshold`, `filter`. **Risk: low**

### hybrid_search
Semantic + keyword search with configurable weights. Parameters: `collection`, `query`, `limit`, `semantic_weight`, `keyword_weight`, `filter`. **Risk: low**

### get
Retrieve documents by IDs. Parameters: `collection`, `ids`. **Risk: low**

### delete
Delete documents by IDs or metadata filter. Parameters: `collection`, `ids`, `filter`. **Risk: medium**

### update_metadata
Update metadata on existing documents. Parameters: `collection`, `ids`, `metadata`. **Risk: low**

### count
Count documents in a collection. Parameters: `collection`, `filter`. **Risk: low**

---

## Chunking Strategies

| Strategy | Split by | Default size | Best for |
|----------|----------|-------------|----------|
| `fixed` | N characters + overlap | 500 chars | Structured data, code |
| `sentence` | Sentence boundaries (`.!?`) | 500 chars | Natural text |
| `paragraph` | Double newlines (`\n\n`) | 1000 chars | Articles, documentation |
| `recursive` | `\n\n` → `\n` → `. ` → ` ` → char | 500 chars | Universal (default) |

Each chunk includes: `text`, `index`, `start_char`, `end_char`, `metadata`.

---

## Embedding Model

| Property | Value |
|----------|-------|
| Model | `paraphrase-multilingual-MiniLM-L12-v2` |
| Dimensions | 384 |
| Languages | ~50 |
| Source | FastEmbed (ONNX, CPU-optimized) |
| Sharing | Singleton shared with `context_builder` |

---

## Configuration

```yaml
modules:
  vector:
    config:
      embedding_model: null          # null = same as context_builder
      default_chunk_size: 500
      default_overlap: 50
      persistence_dir: null          # null = in-memory
```
---

## Aliases (FR/EN)

| Action | Aliases |
|--------|---------|
| `create_collection` | `creer_collection`, `new_collection` |
| `delete_collection` | `supprimer_collection` |
| `list_collections` | `lister_collections` |
| `add` | `ajouter`, `indexer`, `embed`, `insert` |
| `add_file` | `indexer_fichier`, `embed_file` |
| `search` | `rechercher`, `chercher`, `query`, `find_similar` |
| `hybrid_search` | `recherche_hybride` |
| `get` | `obtenir_documents`, `retrieve` |
| `delete` | `supprimer_documents`, `remove` |
| `update_metadata` | `modifier_metadata` |
| `count` | `compter`, `count_docs` |
| `collection_stats` | `statistiques_collection`, `info_collection` |
