---
id: rag
title: RAG Module
sidebar_label: rag
sidebar_position: 15
description: Production-grade RAG - 14 actions, 5 vector backends, hybrid retrieval, citations, semantic cache, Text2SQL, multi-query, CRAG.
---

# rag

Production-grade Retrieval-Augmented Generation module. Manages knowledge bases with hybrid retrieval (BM25 + semantic + RRF), cross-encoder reranking, source citations, semantic caching, and multi-source ingestion.

| Property | Value |
|----------|-------|
| **Module ID** | `rag` |
| **Version** | `1.0.0` |
| **Platforms** | All |
| **Dependencies** | `fastembed`, `qdrant-client` (included) |
| **Optional** | `chromadb`, `lancedb`, `pinecone`, `asyncpg` + `pgvector` |

---

## Design Philosophy

- **Zero-config** - `rag: {}` gives you hybrid RAG with caching and citations out of the box
- **Knowledge bases** - each KB has its own BM25 index, content hashes, and metadata, on top of a shared vector backend
- **Multi-source** - files, databases, and web content are unified into the same retrieval pipeline
- **Citations by default** - every result carries source provenance, injected into LLM context
- **Incremental** - content hashing skips unchanged files, database sync tracks row-level changes

---

## Actions (14)

### create_knowledge_base

Create a new knowledge base (vector collection + BM25 index).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | yes | - | Knowledge base name (1-128 chars) |
| `description` | string | no | `""` | Human-readable description |
| `embedding_model` | string | no | `""` | Override the default embedding model. Empty = use module default |

**Risk:** medium

**Returns:**
```json
{
  "knowledge_base": "docs",
  "created": true,
  "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
  "dimensions": 384
}
```

If the KB already exists: `{"created": false, "already_exists": true}`.

---

### delete_knowledge_base

Delete a knowledge base and all its data. **Irreversible.**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | yes | Knowledge base name to delete |

**Risk:** high

**Returns:**
```json
{
  "knowledge_base": "docs",
  "deleted": true
}
```

---

### list_knowledge_bases

List all knowledge bases with their stats.

No parameters.

**Risk:** low

**Returns:**
```json
{
  "knowledge_bases": [
    {
      "name": "docs",
      "description": "Technical documentation",
      "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
      "dimensions": 384,
      "doc_count": 42,
      "chunk_count": 156,
      "bm25_terms": 2340,
      "created_at": 1711234567.89
    }
  ],
  "count": 1
}
```

---

### knowledge_base_stats

Get detailed statistics for a knowledge base.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | yes | Knowledge base name |

**Risk:** low

**Returns:**
```json
{
  "name": "docs",
  "description": "Technical documentation",
  "embedding_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
  "dimensions": 384,
  "doc_count": 42,
  "chunk_count": 156,
  "backend_count": 156,
  "bm25_terms": 2340,
  "bm25_docs": 156,
  "content_hashes": 42,
  "created_at": 1711234567.89,
  "backend_type": "qdrant",
  "pipeline": {
    "retrieval": "hybrid",
    "reranker": false,
    "bm25_weight": 0.3,
    "semantic_weight": 0.7,
    "rerank_top_n": 20,
    "final_top_k": 5
  },
  "cache": {
    "entries": 127,
    "total_queries": 450,
    "hit_rate": 0.34,
    "hits": 153,
    "misses": 297,
    "evictions": 12
  }
}
```

---

### ingest

Ingest raw text documents into a knowledge base.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `knowledge_base` | string | yes | - | Target knowledge base name |
| `documents` | list[string] | yes | - | Text documents to ingest |
| `ids` | list[string] | no | `null` | Document IDs (auto-generated if omitted) |
| `metadata` | list[dict] | no | `null` | Per-document metadata |
| `source_type` | string | no | `"manual"` | Source type: manual, file, database, web |
| `source_id` | string | no | `""` | Source identifier for citations |

**Risk:** medium

**Returns:**
```json
{
  "knowledge_base": "docs",
  "added": 5,
  "ids": ["d1", "d2", "d3", "d4", "d5"]
}
```

---

### ingest_file

Ingest a file with automatic format detection and chunking.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `knowledge_base` | string | yes | - | Target knowledge base |
| `path` | string | yes | - | File path |
| `chunk_strategy` | string | no | `""` | Override: fixed, sentence, paragraph, recursive |
| `chunk_size` | int | no | `0` | Override chunk size (0 = use default) |
| `chunk_overlap` | int | no | `-1` | Override overlap (-1 = use default) |
| `metadata` | dict | no | `null` | Extra metadata for all chunks |

**Risk:** medium

**Returns:**
```json
{
  "knowledge_base": "docs",
  "file": "/path/to/guide.md",
  "chunks": 12,
  "added": 12,
  "strategy": "recursive"
}
```

If file is unchanged (same content hash): `{"skipped": "unchanged"}`.

---

### ingest_directory

Ingest all matching files from a directory.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `knowledge_base` | string | yes | - | Target knowledge base |
| `path` | string | yes | - | Directory path |
| `extensions` | list[string] | no | `[".md", ".txt", ".pdf"]` | File extensions to include |
| `recursive` | bool | no | `true` | Recurse into subdirectories |
| `max_files` | int | no | `1000` | Maximum files to process |
| `chunk_strategy` | string | no | `""` | Override chunking strategy |
| `chunk_size` | int | no | `0` | Override chunk size |
| `chunk_overlap` | int | no | `-1` | Override overlap |

**Risk:** medium

**Returns:**
```json
{
  "knowledge_base": "docs",
  "directory": "/path/to/docs",
  "documents": 23,
  "chunks": 156,
  "added": 156
}
```

---

### ingest_database

Ingest database table schemas and/or row content.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `knowledge_base` | string | yes | Target knowledge base |
| `connection_id` | string | yes | Database connection ID |
| `tables` | dict | yes | Table configs: `{table_name: {columns, mode, template, max_rows}}` |

**Risk:** medium

**Returns:**
```json
{
  "knowledge_base": "crm_data",
  "connection_id": "crm",
  "schema_docs": 4,
  "row_docs": 1250,
  "added": 1254
}
```

---

### query

Search a knowledge base using the configured retrieval pipeline.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `knowledge_base` | string | yes | - | Knowledge base to search |
| `query` | string | yes | - | Search query |
| `top_k` | int | no | `5` | Results to return (1-100) |
| `min_score` | float | no | `0.0` | Minimum relevance score (0.0-1.0) |
| `strategy` | string | no | `""` | Override: hybrid, semantic, bm25, adaptive |
| `filter` | dict | no | `null` | Metadata filter (backend-specific) |

**Risk:** low

**Returns:**
```json
{
  "knowledge_base": "docs",
  "query": "how does authentication work?",
  "strategy": "hybrid",
  "cache_hit": false,
  "results": [
    {
      "text": "Authentication uses JWT tokens with RSA-256...",
      "score": 0.92,
      "doc_id": "auth_md_chunk_3",
      "citation": {
        "source_type": "file",
        "source_id": "docs/auth.md",
        "location": "section: Overview, chunk 3",
        "confidence": 0.92
      }
    }
  ],
  "count": 5,
  "context_block": "## Retrieved context - cite sources using [1], [2]...\n\n[1] (source: docs/auth.md, section: Overview, confidence: 0.92)\nAuthentication uses JWT tokens..."
}
```

When cache is hit: `"strategy": "cache_hit"`, `"cache_hit": true`.

---

### multi_query

Search with LLM-generated query variants for broader recall.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `knowledge_base` | string | yes | - | Knowledge base to search |
| `query` | string | yes | - | Search query |
| `top_k` | int | no | `5` | Results to return |
| `num_variants` | int | no | `3` | Query variants to generate (2-10) |
| `min_score` | float | no | `0.0` | Minimum relevance score |
| `filter` | dict | no | `null` | Metadata filter |

**Risk:** low

**Returns:**
```json
{
  "knowledge_base": "docs",
  "query": "how does auth work?",
  "strategy": "multi_query",
  "num_variants": 3,
  "results": [...],
  "count": 5,
  "context_block": "..."
}
```

---

### sql_query

Answer a natural language question by generating and executing SQL.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | yes | - | Natural language question |
| `connection_id` | string | yes | - | Database connection ID |
| `knowledge_base` | string | no | `""` | KB with schema info (auto-detect if empty) |
| `top_k` | int | no | `5` | Max results |

**Risk:** low (only SELECT queries are executed)

**Returns:**
```json
{
  "query": "how many active users?",
  "connection_id": "crm",
  "strategy": "text2sql",
  "results": [
    {
      "text": "SQL: SELECT count(*) FROM users WHERE active = true\n\n| count |\n|-------|\n| 11203 |",
      "score": 1.0,
      "doc_id": "sql_...",
      "citation": {
        "source_type": "database",
        "source_id": "crm",
        "location": "SELECT count(*) FROM users WHERE active = true"
      }
    }
  ],
  "count": 1
}
```

---

### clear_cache

Clear the semantic cache.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `knowledge_base` | string | no | `""` | Clear for a specific KB. Empty = clear all |

**Risk:** low

**Returns:**
```json
{
  "cleared": 127,
  "cache_enabled": true,
  "remaining_entries": 0,
  "total_queries": 450,
  "hit_rate": 0.34
}
```

---

### migrate_embeddings

Re-embed a knowledge base with a different embedding model. Processes in batches of 256.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `knowledge_base` | string | yes | Knowledge base to migrate |
| `target_model` | string | yes | New embedding model (shortcut or ID) |

**Risk:** high

**Returns:**
```json
{
  "knowledge_base": "docs",
  "migrated": true,
  "re_embedded": 156,
  "old_model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
  "new_model": "BAAI/bge-m3",
  "new_dimensions": 1024
}
```

If target model is the same: `{"migrated": false, "reason": "already using this model"}`.

---

### list_models

List available embedding models.

No parameters.

**Risk:** low

**Returns:**
```json
{
  "models": [
    {
      "shortcut": "minilm-l12",
      "fastembed_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
      "dimensions": 384,
      "description": "Fast multilingual, 50 langs, 220 MB",
      "is_default": true
    },
    {
      "shortcut": "bge-m3",
      "fastembed_id": "BAAI/bge-m3",
      "dimensions": 1024,
      "description": "SOTA multilingual, 100+ langs, 2.3 GB",
      "is_default": false
    }
  ],
  "current_default": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
  "current_dimensions": 384
}
```

---

## Architecture

```
                          ┌─────────────────────┐
                          │     RagModule        │
                          │   (14 actions)       │
                          └──────────┬──────────┘
                                     │
        ┌────────────────┬───────────┼───────────┬────────────────┐
        │                │           │           │                │
   QueryRouter     SemanticCache  RagPipeline  IndexingEngine  CitationTracker
   (classify)      (lookup/store) (orchestrate) (scan/ingest)  (provenance)
        │                │           │           │
        │                │     ┌─────┼─────┐     ├── Ingestors (9 formats)
        │                │     │     │     │     └── SyncStrategies (3)
        │                │  Semantic BM25  Hybrid
        │                │  Search   Index  (RRF)
        │                │     │     │
        │                │     ▼     │
        │                │  VectorBackend ◄──── Qdrant | Chroma | LanceDB
        │                │  (5 backends)        Pinecone | pgvector
        │                │
        │                └── EmbeddingManager (7 models + custom)
        │
        └── Strategies: MultiQuery | Text2SQL | CRAG | Adaptive
```

### Component responsibilities

| Component | File | Purpose |
|-----------|------|---------|
| `RagModule` | `module.py` | Action dispatch, KB management, lifecycle |
| `EmbeddingManager` | `embeddings.py` | Model resolution, lazy loading, embedding |
| `BM25Index` | `bm25.py` | Okapi BM25 keyword search (pure Python) |
| `RagPipeline` | `pipeline.py` | Retrieval orchestration (semantic, BM25, hybrid) |
| `CrossEncoderReranker` | `reranker.py` | Cross-encoder re-scoring |
| `SemanticCache` | `cache.py` | Query-level result caching by embedding similarity |
| `QueryRouter` | `router.py` | Fast query classification (<5ms) |
| `CitationTracker` | `citations.py` | Provenance tracking, context block formatting |
| `IndexingEngine` | `indexing/engine.py` | File scanning, incremental hashing, DB indexing |
| `Ingestors` | `indexing/ingestors.py` | Format-specific document extraction |
| `SyncStrategies` | `indexing/sync.py` | Database change detection |
| `RRF/WeightedFusion` | `fusion.py` | Result list merging |
| `VectorBackend` | `backends/base.py` | Abstract vector DB protocol |
| `HybridStrategy` | `strategies/hybrid.py` | BM25 + semantic + RRF |
| `MultiQueryStrategy` | `strategies/multiquery.py` | LLM query expansion |
| `Text2SQLStrategy` | `strategies/text2sql.py` | NL → SQL → results |
| `CRAGStrategy` | `strategies/crag.py` | Quality evaluation + fallback |
| `AdaptiveStrategy` | `strategies/adaptive.py` | Router-based strategy selection |

---

## Configuration Models

All configuration is validated at compile time via Pydantic:

| Config class | Key | Description |
|-------------|-----|-------------|
| `RagConfig` | `rag:` | Root config |
| `BackendConfig` | `backend:` | Vector DB selection |
| `PipelineConfig` | `pipeline:` | Retrieval strategy, weights, reranking |
| `ChunkingConfig` | `chunking:` | Text splitting parameters |
| `CacheConfig` | `cache:` | Semantic cache settings |
| `CitationConfig` | `citations:` | Source provenance injection |
| `MultiQueryConfig` | `pipeline.multi_query:` | Query expansion |
| `Text2SQLConfig` | `text2sql:` | NL-to-SQL |
| `CragConfig` | `crag:` | Corrective RAG |
| `AdaptiveConfig` | `adaptive:` | Auto-routing |
| `FileSourceConfig` | `sources[type=file]:` | File source definition |
| `DatabaseSourceConfig` | `sources[type=database]:` | DB source definition |
| `TableConfig` | `tables.<name>:` | Per-table indexing config |
| `DatabaseSyncConfig` | `sync:` | DB change detection |

---

## Chunking Strategies

| Strategy | Split by | Default size | Best for |
|----------|----------|-------------|----------|
| `fixed` | N characters + overlap | 500 chars | Structured data, code |
| `sentence` | Sentence boundaries (`.!?`) | 500 chars | Natural text |
| `paragraph` | Double newlines (`\n\n`) | 1000 chars | Articles, documentation |
| `recursive` | `\n\n` → `\n` → `. ` → ` ` → char | 500 chars | Universal **(default)** |

---

## Testing

206 tests cover every component:

```bash
pytest tests/test_rag_module.py -v
```

| Test area | Count |
|-----------|-------|
| BM25 | 11 |
| Fusion (RRF + weighted) | 9 |
| Citations | 11 |
| Query Router | 8 |
| Semantic Cache | 11 |
| Config validation | 6 |
| Parameter models | 16 |
| Embedding Manager | 11 |
| Reranker | 4 |
| Ingestors (9 formats) | 15 |
| Indexing Engine | 15 |
| Sync Strategies | 9 |
| Vector Backends (5) | 21 |
| Retrieval Strategies | 21 |
| Pipeline | 7 |
| Module Actions (14) | 18 |
| **Total** | **206** |
