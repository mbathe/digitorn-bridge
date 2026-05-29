---
id: rag
title: rag Module
sidebar_label: rag
sidebar_position: 15
description: Production-grade RAG - 14 actions, 6 vector backends, hybrid retrieval, citations, semantic cache, Text2SQL, multi-query, CRAG.
---

# rag

Production-grade Retrieval-Augmented Generation. Knowledge
bases with hybrid retrieval (BM25 + semantic + RRF),
cross-encoder reranking, source citations, semantic cache,
multi-source ingestion, Text2SQL, multi-query expansion, CRAG
fallback.

| Property | Value |
|----------|-------|
| Module id | `rag` |
| Type | shared (one instance per daemon, per-app reconfig via `on_config_update`) |
| Action count | 14 |
| Pip deps | `fastembed`, `qdrant-client` (bundled) |
| Optional pip deps | `chromadb`, `lancedb`, `pinecone`, `asyncpg + pgvector`, `elasticsearch` |

> **Full reference** (every config field, all 6 backends, all
> 7 embedding models + 5 reranker models, ingestion formats,
> sync strategies, complete enterprise example):
> [RAG Module](../../language/37-rag.md). This page is a
> quick module summary.

## The 14 actions

 All `risk_level` mostly `medium` or
`high` for ingestion / migration; `low` for queries / stats.

| Tool | Source | Purpose |
|------|--------|---------|
| `rag.create_knowledge_base` | | Create a named KB. |
| `rag.delete_knowledge_base` | | Drop a KB + its vector + BM25 indexes. |
| `rag.list_knowledge_bases` | | Enumerate KBs with metadata. |
| `rag.knowledge_base_stats` | | Counts, model, last sync, hit rate. |
| `rag.ingest` | | Add raw text documents. |
| `rag.ingest_file` | | Add a single file. |
| `rag.ingest_directory` | | Walk a directory + index matching files (content-hash dedup). |
| `rag.ingest_database` | | Index DB tables (rows or schema-only). |
| `rag.query` | | Retrieve from a KB (default strategy or per-call override). |
| `rag.multi_query` | | LLM-expanded query with RRF fusion. |
| `rag.sql_query` | | Text2SQL - generate + execute a SELECT. |
| `rag.clear_cache` | | Wipe the semantic cache. |
| `rag.migrate_embeddings` | | Switch a KB to a new embedding model (re-embeds in batches). |
| `rag.list_models` | | List available embedding + reranker shortcuts. |

## Zero-config quick start

```yaml
tools:
  modules:
    rag: {}
```

Defaults:

| Setting | Default |
|---------|---------|
| Embedding | `minilm-l12` (384 d, multilingual, 220 MB) |
| Backend | Qdrant in-memory |
| Strategy | Hybrid (BM25 + semantic + RRF) |
| Chunking | recursive, 500 chars, 50 overlap |
| Cache | enabled, in-memory, 1 h TTL |
| Citations | enabled, inline |
| Reranker | disabled |

## 6 vector backends

`BackendConfig.type`:

| Backend | Mode | Pip dep |
|---------|------|---------|
| **Qdrant** *(default)* | embedded / remote | bundled |
| **ChromaDB** | embedded / remote | `chromadb` |
| **LanceDB** | embedded (file) | `lancedb`, `pyarrow` |
| **Pinecone** | cloud | `pinecone` |
| **pgvector** | PostgreSQL | `asyncpg`, `pgvector` |
| **Elasticsearch** | remote cluster | `elasticsearch` |

## 7 embedding models

`BUILTIN_MODELS`. All auto-downloaded
by FastEmbed (ONNX, CPU):

| Shortcut | FastEmbed id | Dims |
|----------|--------------|:----:|
| `minilm-l12` *(default)* | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 384 |
| `bge-m3` | `BAAI/bge-m3` | 1024 |
| `bge-small` | `BAAI/bge-small-en-v1.5` | 384 |
| `bge-large` | `BAAI/bge-large-en-v1.5` | 1024 |
| `nomic-v1.5` | `nomic-ai/nomic-embed-text-v1.5` | 768 |
| `jina-v3` | `jinaai/jina-embeddings-v3` | 1024 |
| `snowflake-xs` | `snowflake/snowflake-arctic-embed-xs` | 384 |

Custom models: any FastEmbed-supported HuggingFace id.

## 5 reranker models

`BUILTIN_RERANKERS`. Default
`minilm-l6`:

| Shortcut | HF id |
|----------|-------|
| `minilm-l6` *(default)* | `Xenova/ms-marco-MiniLM-L-6-v2` |
| `minilm-l12` | `Xenova/ms-marco-MiniLM-L-12-v2` |
| `bge-reranker-base` | `BAAI/bge-reranker-base` |
| `jina-reranker-v1-tiny` | `jinaai/jina-reranker-v1-tiny-en` |
| `jina-reranker-v2` | `jinaai/jina-reranker-v2-base-multilingual` |

```yaml
config:
  reranker: true                  # default minilm-l6
  # OR
  reranker: "bge-reranker-base"
```

## Configuration shape

```yaml
tools:
  modules:
    rag:
      config:
        embedding_model: minilm-l12
        reranker: false                 # true | "<shortcut>" | "<HF id>"
        backend:
          type: qdrant                  # qdrant | chroma | lancedb | pinecone | pgvector | elasticsearch
          path: ""                      # "" = in-memory
          url: ""
          quantization: none            # none | int8 | binary (qdrant only)
        pipeline:
          retrieval: hybrid             # hybrid | semantic | bm25
          bm25_weight: 0.3
          semantic_weight: 0.7
          rerank_top_n: 20
          final_top_k: 5
          multi_query:
            enabled: false
            provider: ""
            num_variants: 3
        chunking:
          strategy: recursive           # fixed | sentence | paragraph | recursive
          size: 500
          overlap: 50
        sources:
          - type: file
            path: "{{workspace}}/docs"
            extensions: [.md, .txt, .pdf]
            watch: true
          - type: database
            connection_id: crm
            sync: { strategy: updated_at, interval: 30 }
            tables:
              users:
                columns: [id, name, email, bio]
                mode: embed_rows
                template: "{name} - {bio}"
              orders:
                mode: schema_only
        cache:
          enabled: true
          backend: memory               # memory | redis
          similarity_threshold: 0.95
          ttl: 3600
        citations:
          enabled: true
          format: inline                # inline | footnote | structured
          verify: false
        text2sql:
          enabled: false
          provider: ""
          example_cache: true
        crag:
          enabled: false
          confidence_threshold: 0.5
          fallback: broader_query       # broader_query | none
        adaptive:
          enabled: false
          strategies: {}
        contextual_retrieval:
          enabled: false
          provider: ""
          concurrency: 5
        max_knowledge_bases: 50
        max_documents: 100000
        persistence_dir: ""
```

## Shared module + per-app reconfig (gotcha)

`rag` has `isolation = "shared"` - one instance per daemon.
`on_start` runs **once** at boot with empty config →
default in-memory backend.

When an app activates, the bootstrap calls
`module.on_config_update(cfg)` with the app's config. The
overridden hook (in):

1. Compares old vs new backend path.
2. Closes the old backend if changed.
3. Re-creates + initialises the new backend.
4. Calls `_discover_existing_collections` to rebuild
   `_kbs` from existing on-disk collections.

> **Common config bug**: forgetting the `config:` wrapper
> under `tools.modules.rag` causes `compiled.modules["rag"].config`
> to be `{}`, so `on_config_update` is never called and every
> query returns *"knowledge base not found"*. Always nest
> under `config:`.

## Cross-references

- Full RAG reference (every adapter, sync strategy,
  ingestion format, complete enterprise example):
  [RAG Module](../../language/37-rag.md)
- App-config block reference (`tools.modules.rag.config`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- Lower-level vector ops module (no RAG pipeline):
  [vector reference](vector.md)
- System-level workspace index (separate from `rag`):
  [index reference](index_module.md)
