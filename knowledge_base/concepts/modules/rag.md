---
id: module-concept-rag
title: "rag module — overview"
type: module-concept
module: rag
isolation: shared
keywords: [rag, rag-module, create_knowledge_base, delete_knowledge_base, list_knowledge_bases, knowledge_base_stats, ingest, ingest_file, query, multi_query, sql_query, ingest_directory, ingest_database, clear_cache, migrate_embeddings, list_models]
version: 1.0.0
---

# `rag` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 14 visible, 0 internal

## Description (from class docstring)

RAG module — optional, only loaded when declared in app YAML.

## Configuration

Set under `modules.rag.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon at module init time. Do NOT set manually in YAML — the daemon resolves it from the app's workspace/workspace_mode config. |
| `embedding_model` | str \| digitorn.modules.rag.config.CustomEmbeddingConfig |  | `'minilm-l12'` | Shortcuts: minilm-l12, bge-m3, bge-small, nomic-v1.5, jina-v3. Or any FastEmbed model ID. Or {id, dimensions, pooling, model_file}. |
| `reranker` | bool \| str |  | `False` | true = default reranker (minilm-l6). Or a FastEmbed reranker model ID. |
| `backend` | BackendConfig |  | `BackendConfig` (nested — see module code) |  |
| `pipeline` | PipelineConfig |  | `PipelineConfig` (nested — see module code) |  |
| `chunking` | ChunkingConfig |  | `ChunkingConfig` (nested — see module code) |  |
| `sources` | list |  | `[]` |  |
| `auto_index` | AutoIndexConfig |  | `AutoIndexConfig` (nested — see module code) |  |
| `cache` | CacheConfig |  | `CacheConfig` (nested — see module code) |  |
| `citations` | CitationConfig |  | `CitationConfig` (nested — see module code) |  |
| `contextual_retrieval` | ContextualRetrievalConfig |  | `ContextualRetrievalConfig` (nested — see module code) |  |
| `text2sql` | Text2SQLConfig |  | `Text2SQLConfig` (nested — see module code) |  |
| `crag` | CragConfig |  | `CragConfig` (nested — see module code) |  |
| `adaptive` | AdaptiveConfig |  | `AdaptiveConfig` (nested — see module code) |  |
| `max_knowledge_bases` | int |  | `50` |  |
| `max_documents` | int |  | `100000` |  |
| `persistence_dir` | str |  | `''` |  |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `create_knowledge_base` | `RagCreateKnowledgeBase` |  | low | Create a new knowledge base for storing and searching documents. |
| `delete_knowledge_base` | `RagDeleteKnowledgeBase` |  | high | Delete a knowledge base and all its data. |
| `list_knowledge_bases` | `RagListKnowledgeBases` |  | low | List all knowledge bases with their stats. |
| `knowledge_base_stats` | `RagKnowledgeBaseStats` |  | low | Get detailed statistics for a knowledge base. |
| `ingest` | `RagIngest` |  | low | Ingest raw text documents into a knowledge base. |
| `ingest_file` | `RagIngestFile` |  | low | Ingest a file into a knowledge base with automatic chunking. |
| `query` | `RagQuery` |  | low | Search a knowledge base using the configured retrieval pipeline with citations. |
| `multi_query` | `RagMultiQuery` |  | low | Search with LLM-generated query variants for broader recall (MultiQuery RAG). |
| `sql_query` | `RagSqlQuery` |  | medium | Answer a natural language question by generating and executing SQL. |
| `ingest_directory` | `RagIngestDirectory` |  | low | Ingest all matching files from a directory into a knowledge base. |
| `ingest_database` | `RagIngestDatabase` |  | medium | Ingest database tables into a knowledge base (schema and/or rows). |
| `clear_cache` | `RagClearCache` |  | low | Clear the semantic cache for faster-but-stale response prevention. |
| `migrate_embeddings` | `RagMigrateEmbeddings` |  | high | Re-embed a knowledge base with a different embedding model. |
| `list_models` | `RagListModels` |  | low | List available embedding models (built-in shortcuts and current default). |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: rag
      actions: [create_knowledge_base, delete_knowledge_base, list_knowledge_bases, knowledge_base_stats, ingest, ingest_file, query, multi_query, sql_query, ingest_directory, ingest_database, clear_cache, migrate_embeddings, list_models]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {rag: [create_knowledge_base, delete_knowledge_base, list_knowledge_bases, knowledge_base_stats, ingest]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/rag-*.md`.
