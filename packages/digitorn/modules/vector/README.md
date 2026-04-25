# Vector Module

RAG-native vector collections with FastEmbed and Qdrant.

## Overview

The Vector module lets agents create vector collections, index documents,
and perform semantic search. It shares the FastEmbed model singleton with
`context_builder` — no extra memory or model loading.

Collections are named `user_{app_id}_{name}` to avoid collision with the
system `tools` collection used by context_builder.

## Key Features

- **Shared embedding model** — reuses `context_builder`'s FastEmbed singleton (`paraphrase-multilingual-MiniLM-L12-v2`, 384 dims, ~50 languages)
- **4 chunking strategies** — fixed, sentence, paragraph, recursive (LangChain-style)
- **Hybrid search** — semantic (Qdrant ANN) + keyword (token overlap), configurable weights
- **File indexing** — `add_file` reads, chunks, embeds, and indexes any text file
- **Metadata filtering** — Qdrant payload filters on custom metadata fields
- **In-memory or on-disk** — Qdrant embedded with optional persistence directory

## Actions (12)

| Action | Description | Risk |
|--------|-------------|------|
| **Collections** | | |
| `create_collection` | Create a named vector collection | Medium |
| `delete_collection` | Delete a collection and all its data | High |
| `list_collections` | List all user collections | Low |
| `collection_stats` | Collection stats: count, dimensions, size | Low |
| **Documents** | | |
| `add` | Add text documents with chunking and embedding | Medium |
| `add_file` | Read a file, chunk it, and index all chunks | Medium |
| `get` | Retrieve documents by IDs | Low |
| `delete` | Delete documents by IDs or filter | Medium |
| `update_metadata` | Update metadata on existing documents | Low |
| `count` | Count documents in a collection | Low |
| **Search** | | |
| `search` | Semantic search using vector similarity | Low |
| `hybrid_search` | Semantic + keyword search with configurable weights | Low |

## Chunking Strategies

| Strategy | Split by | Best for |
|----------|----------|----------|
| `fixed` | N characters + overlap | Structured data, code |
| `sentence` | Sentence boundaries (`.!?`) | Natural text |
| `paragraph` | Double newlines (`\n\n`) | Articles, documentation |
| `recursive` | `\n\n` → `\n` → `. ` → ` ` → char | Universal (default) |

## Architecture

```
VectorModule
    │
    ├── FastEmbed model (shared with context_builder)
    │       └── _get_model() singleton
    │
    ├── Qdrant embedded
    │       └── Collections: user_{app_id}_{name}
    │               └── Payload: {text, source, chunk_index, metadata, added_at}
    │
    ├── Chunking (chunking.py)
    │       ├── fixed_chunks()
    │       ├── sentence_chunks()
    │       ├── paragraph_chunks()
    │       └── recursive_chunks()
    │
    └── Hybrid search
            semantic_weight × semantic_score + keyword_weight × keyword_score
```

## App YAML Configuration

```yaml
modules:
  vector:
    config:
      embedding_model: null          # null = same as context_builder
      default_chunk_size: 500
      default_overlap: 50
      persistence_dir: null          # null = in-memory
```

## LLM Usage

```
1. vector.create_collection  →  create "docs" collection
2. vector.add_file           →  index a document (auto-chunked)
3. vector.search             →  semantic query
4. vector.hybrid_search      →  semantic + keyword for precision
5. vector.collection_stats   →  monitor index size
```
