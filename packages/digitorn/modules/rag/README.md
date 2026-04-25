# RAG Module

Advanced Retrieval-Augmented Generation with hybrid search, re-ranking, citations,
semantic cache, and multi-source ingestion.

## Overview

The RAG module gives AI agents a complete knowledge retrieval pipeline:

- **Hybrid search** — BM25 keyword + semantic vector search fused via Reciprocal Rank Fusion
- **Cross-encoder reranking** — FastEmbed TextCrossEncoder for precision
- **Semantic cache** — cosine-similarity lookup on query embeddings, sub-15ms hits
- **Citations** — every retrieved chunk carries full provenance (source, location, confidence)
- **Multi-source** — files (MD, PDF, code, CSV, XLSX, JSON, HTML), databases, web
- **Text2SQL** — natural language to SQL via the database module
- **CRAG** — Corrective RAG with automatic fallback on low-confidence results
- **Zero-config** — `rag: {}` gives you Qdrant in-memory, MiniLM-L12, hybrid search

## Actions

| Action | Description | Risk |
|--------|-------------|------|
| `create_knowledge_base` | Create a new knowledge base | Low |
| `delete_knowledge_base` | Delete a knowledge base and all data | High |
| `list_knowledge_bases` | List all knowledge bases with stats | Low |
| `knowledge_base_stats` | Detailed statistics for a knowledge base | Low |
| `ingest` | Ingest raw text documents | Low |
| `ingest_file` | Ingest a file with auto-chunking | Low |
| `ingest_directory` | Ingest all files from a directory | Low |
| `ingest_database` | Ingest database schema and/or rows | Medium |
| `query` | Search with the full retrieval pipeline + citations | Low |
| `multi_query` | Multi-variant search (LLM query expansion) | Low |
| `sql_query` | Natural language to SQL execution | Medium |
| `clear_cache` | Clear the semantic cache | Low |
| `migrate_embeddings` | Re-embed a KB with a different model | High |
| `list_models` | List available embedding models | Low |

## Quick Start

```yaml
# Zero-config — works out of the box
modules:
  rag: {}
```

```yaml
# Full pipeline
modules:
  rag:
    embedding_model: bge-m3
    reranker: bge-reranker-v2
    pipeline:
      retrieval: hybrid
      multi_query:
        enabled: true
        provider: helper
    cache:
      enabled: true
    citations:
      enabled: true
      verify: true
```

## Embedding Models

| Shortcut | Model | Dimensions | Languages |
|----------|-------|------------|-----------|
| `minilm-l12` | paraphrase-multilingual-MiniLM-L12-v2 | 384 | 50+ |
| `bge-m3` | BAAI/bge-m3 | 1024 | 100+ |
| `bge-small` | BAAI/bge-small-en-v1.5 | 384 | EN |
| `bge-large` | BAAI/bge-large-en-v1.5 | 1024 | EN |
| `nomic-v1.5` | nomic-ai/nomic-embed-text-v1.5 | 768 | EN |
| `jina-v3` | jinaai/jina-embeddings-v3 | 1024 | Multi |
| `snowflake-xs` | snowflake/arctic-embed-xs | 384 | EN |

## Vector Backends

- **Qdrant** (default) — in-memory or persistent, quantization support
- **ChromaDB** — lightweight, great for prototyping
- **LanceDB** — serverless, columnar
- **Pinecone** — managed cloud
- **pgvector** — PostgreSQL extension

## Dependencies

Required: `fastembed`, `qdrant-client` (both in default install).
Optional backends: `chromadb`, `lancedb`, `pinecone`, `asyncpg`.
