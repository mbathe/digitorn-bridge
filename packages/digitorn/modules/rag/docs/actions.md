# RAG Module - Action Reference

Complete reference for all 14 actions exposed by the RAG module.

---

## create_knowledge_base

Create a new knowledge base for storing and searching documents.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `name` | string | yes | - | Knowledge base name (alphanumeric + hyphens). |
| `description` | string | no | `""` | Human-readable description. |
| `embedding_model` | string | no | module default | Override the embedding model for this KB. |

### Returns

```json
{
  "success": true,
  "knowledge_base": "my-docs",
  "created": true,
  "embedding_model": "minilm-l12",
  "dimensions": 384
}
```

If the KB already exists, returns `created: false, already_exists: true`.

---

## delete_knowledge_base

Delete a knowledge base and all its data (vectors, BM25 index, cache entries).

**Risk level:** High

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `name` | string | yes | - | Knowledge base to delete. |

---

## list_knowledge_bases

List all knowledge bases with summary stats.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `verbose` | boolean | no | `false` | Include detailed stats per KB. |

### Returns

```json
{
  "knowledge_bases": [
    {
      "name": "my-docs",
      "description": "Technical documentation",
      "doc_count": 42,
      "chunk_count": 350,
      "embedding_model": "minilm-l12"
    }
  ],
  "count": 1
}
```

---

## knowledge_base_stats

Get detailed statistics for a specific knowledge base.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `name` | string | yes | - | Knowledge base name. |

---

## ingest

Ingest raw text documents into a knowledge base with automatic chunking and embedding.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `knowledge_base` | string | no | `"default"` | Target knowledge base. |
| `documents` | list[string] | yes | - | Text content to ingest. |
| `metadata` | list[dict] | no | `[]` | Per-document metadata. |
| `chunk_size` | integer | no | config default | Override chunk size. |
| `chunk_overlap` | integer | no | config default | Override chunk overlap. |

### Returns

```json
{
  "success": true,
  "knowledge_base": "default",
  "added": 5,
  "chunks_created": 23
}
```

---

## ingest_file

Ingest a single file with automatic format detection and chunking.

Supported formats: `.md`, `.txt`, `.pdf`, `.py`, `.ts`, `.js`, `.csv`, `.xlsx`, `.json`, `.html`.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `knowledge_base` | string | no | `"default"` | Target knowledge base. |
| `path` | string | yes | - | Path to the file. |

---

## ingest_directory

Ingest all matching files from a directory (incremental, skips unchanged files).

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `knowledge_base` | string | no | `"default"` | Target knowledge base. |
| `path` | string | yes | - | Directory path. |
| `extensions` | list[string] | no | all supported | File extensions to include. |
| `recursive` | boolean | no | `true` | Recurse into subdirectories. |
| `max_files` | integer | no | `5000` | Maximum files to process. |

### Returns

```json
{
  "success": true,
  "knowledge_base": "default",
  "documents": 42,
  "chunks_created": 350,
  "skipped": 3,
  "errors": []
}
```

---

## ingest_database

Ingest database tables into a knowledge base (schema only or full row embedding).

**Risk level:** Medium

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `knowledge_base` | string | no | `"default"` | Target knowledge base. |
| `connection_id` | string | yes | - | Database connection ID (from database module). |
| `tables` | dict | no | all tables | Table configurations (`mode: schema_only\|embed_rows`). |

---

## query

Search a knowledge base using the configured retrieval pipeline. Returns results with citations.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `knowledge_base` | string | no | `"default"` | Knowledge base to search. |
| `query` | string | yes | - | Search query (natural language). |
| `top_k` | integer | no | config default | Number of results to return. |

### Returns

```json
{
  "success": true,
  "results": [
    {
      "text": "Employees get 25 days of leave per year...",
      "score": 0.92,
      "citation": {
        "source_type": "file",
        "source_id": "policies/leave.md",
        "location": "section: Annual Leave"
      }
    }
  ],
  "context_block": "## Retrieved context\n\n[1] (source: policies/leave.md)...",
  "cache_hit": false
}
```

---

## multi_query

Search using LLM-generated query variants for broader recall (MultiQuery RAG).
The LLM generates N variants of the original query, searches with each, then fuses results.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `knowledge_base` | string | no | `"default"` | Knowledge base to search. |
| `query` | string | yes | - | Original search query. |
| `num_variants` | integer | no | config default | Number of query variants to generate. |
| `top_k` | integer | no | config default | Results per variant. |

---

## sql_query

Answer a natural language question by generating and executing SQL against a connected database.

**Risk level:** Medium

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `knowledge_base` | string | no | `"default"` | KB with indexed database schema. |
| `question` | string | yes | - | Natural language question. |
| `connection_id` | string | yes | - | Database connection to query. |

### Returns

```json
{
  "success": true,
  "sql": "SELECT department, COUNT(*) as count FROM employees GROUP BY department",
  "rows": [
    {"department": "Engineering", "count": 45},
    {"department": "Marketing", "count": 12}
  ],
  "row_count": 2,
  "citation": {
    "source_type": "database",
    "source_id": "main_db:employees"
  }
}
```

---

## clear_cache

Clear the semantic cache. Use after bulk ingestion or when stale results are suspected.

**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `knowledge_base` | string | no | all | Clear cache for a specific KB, or all. |

---

## migrate_embeddings

Re-embed an entire knowledge base with a different embedding model. The old vectors are replaced.

**Risk level:** High

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `knowledge_base` | string | yes | - | Knowledge base to migrate. |
| `target_model` | string | yes | - | New embedding model shortcut or ID. |

---

## list_models

List available embedding models (built-in shortcuts and the current module default).

**Risk level:** Low

### Returns

```json
{
  "default_model": "minilm-l12",
  "models": {
    "minilm-l12": {"id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "dimensions": 384},
    "bge-m3": {"id": "BAAI/bge-m3", "dimensions": 1024}
  }
}
```
