---
id: rag
title: Advanced RAG Module
sidebar_label: RAG
sidebar_position: 37
description: Production-grade Retrieval-Augmented Generation — multi-source, multi-strategy, citations, semantic cache, Text2SQL, zero-config.
---

# Advanced RAG Module

The `rag` module is a production-grade Retrieval-Augmented Generation engine. It unifies files, databases, and web sources into searchable knowledge bases with citations, semantic caching, and intelligent query routing — all configurable in YAML.

## Why a dedicated RAG module?

The existing `vector` module handles basic vector operations (add, search, delete). The `rag` module builds on top with:

- **Knowledge bases** instead of raw collections (BM25 + semantic + metadata, unified)
- **Hybrid retrieval** with Reciprocal Rank Fusion (RRF) by default
- **Cross-encoder reranking** for precision
- **Source citations** injected into LLM context
- **Semantic cache** for sub-15ms repeated queries
- **Multi-format ingestion** (Markdown, PDF, code, CSV, JSON, HTML, databases)
- **Database sync** (row-level change detection, auto-reindex)
- **Text2SQL** for natural language questions over structured data
- **Multi-query expansion** for broader recall
- **Corrective RAG** (quality evaluation + fallback)
- **5 vector backends** (Qdrant, ChromaDB, LanceDB, Pinecone, pgvector)

---

## Zero-Config Quick Start

```yaml
modules:
  rag: {}
```
This gives you:

| Setting | Default |
|---------|---------|
| Embedding model | `minilm-l12` (384 dims, 50 languages, 220 MB) |
| Vector backend | Qdrant in-memory |
| Retrieval strategy | Hybrid (BM25 + semantic + RRF) |
| Chunking | Recursive, 500 chars, 50 overlap |
| Semantic cache | Enabled, in-memory, 1h TTL |
| Citations | Enabled, inline format |
| Reranker | Disabled |

The agent can then create knowledge bases and ingest documents via tool calls:

```
Agent: I'll create a knowledge base and index your docs folder.
→ rag.create_knowledge_base(name="docs")
→ rag.ingest_directory(knowledge_base="docs", path="./docs", extensions=[".md", ".txt"])
→ rag.query(knowledge_base="docs", query="how does authentication work?")
```

---

## Configuration Reference

### Full YAML Schema

```yaml
modules:
  rag:
    # ── Embedding model ──────────────────────────────────────────
    # Shortcuts: minilm-l12, bge-m3, bge-small, bge-large, nomic-v1.5, jina-v3, snowflake-xs
    # Or any FastEmbed model ID: "BAAI/bge-m3"
    # Or custom: { id: "my-org/model", dimensions: 768, pooling: mean, model_file: "onnx/model.onnx" }
    embedding_model: minilm-l12

    # ── Reranker ─────────────────────────────────────────────────
    # false = disabled, true = default (minilm-l6), or a model ID
    reranker: false

    # ── Vector backend ───────────────────────────────────────────
    backend:
      type: qdrant           # qdrant | chroma | lancedb | pinecone | pgvector
      path: ""               # Persistent storage path. Empty = in-memory
      url: ""                # Remote server URL
      quantization: none     # none | int8 | binary (Qdrant only)
      # Pinecone-specific:
      # api_key: ""
      # index_name: ""
      # cloud: aws
      # region: us-east-1
      # pgvector-specific:
      # dsn: "postgres://user:pass@host/db"

    # ── Retrieval pipeline ───────────────────────────────────────
    pipeline:
      retrieval: hybrid      # hybrid | semantic | bm25
      bm25_weight: 0.3       # Weight for BM25 in hybrid fusion
      semantic_weight: 0.7   # Weight for semantic in hybrid fusion
      rerank_top_n: 20       # Candidates to re-rank (0 = skip reranking)
      final_top_k: 5         # Final results returned
      multi_query:            # Query expansion via LLM
        enabled: false
        provider: ""          # LLM provider_id for variant generation
        num_variants: 3       # 2-10 query variants

    # ── Chunking ─────────────────────────────────────────────────
    chunking:
      strategy: recursive    # fixed | sentence | paragraph | recursive
      size: 500              # 50-10000 characters
      overlap: 50            # 0-500 characters

    # ── Sources (auto-index at startup) ──────────────────────────
    sources:
      # File sources
      - type: file
        path: "{{workspace}}/docs"
        extensions: [.md, .txt, .pdf]
        watch: true           # Re-index on file changes
        recursive: true
        max_files: 1000

      # Database sources
      - type: database
        connection_id: crm    # Reference to a database module connection
        sync:
          strategy: updated_at  # updated_at | changelog | notify
          interval: 30          # Poll interval in seconds
          auto_create_triggers: true  # For changelog strategy
          prune_after_hours: 24
        tables:
          users:
            columns: [id, name, email, bio, department]
            mode: embed_rows
            template: "{name} ({department}) - {bio}"
            sync: updated_at
            max_rows: 50000
          orders:
            mode: schema_only  # DDL only, for Text2SQL

    auto_index:
      on_start: true
      schedule: ""            # Cron expression for periodic re-indexing

    # ── Semantic cache ───────────────────────────────────────────
    cache:
      enabled: true
      backend: memory         # memory | redis
      similarity_threshold: 0.95
      ttl: 3600               # Seconds
      max_entries: 10000
      # redis_url: ""         # When backend=redis

    # ── Citations ────────────────────────────────────────────────
    citations:
      enabled: true
      format: inline          # inline | footnote | structured
      verify: false           # Post-generation citation verification

    # ── Text2SQL ─────────────────────────────────────────────────
    text2sql:
      enabled: false
      provider: ""            # LLM provider_id for SQL generation
      example_cache: true     # Cache validated (question, SQL) pairs

    # ── Corrective RAG ───────────────────────────────────────────
    crag:
      enabled: false
      provider: ""            # LLM provider_id for quality evaluation
      confidence_threshold: 0.5
      fallback: broader_query # broader_query | none

    # ── Adaptive routing ─────────────────────────────────────────
    adaptive:
      enabled: false
      provider: ""
      strategies: {}          # Named strategy configs

    # ── Contextual retrieval ─────────────────────────────────────
    contextual_retrieval:
      enabled: false
      provider: ""
      concurrency: 5
      prompt_template: ""

    # ── Limits ───────────────────────────────────────────────────
    max_knowledge_bases: 50
    max_documents: 100000
    persistence_dir: ""       # State persistence directory
```
---

## Embedding Models

Seven built-in models with auto-download:

| Shortcut | Model ID | Dimensions | Description |
|----------|----------|-----------|-------------|
| `minilm-l12` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 384 | Fast multilingual, 50 langs, 220 MB **(default)** |
| `bge-m3` | `BAAI/bge-m3` | 1024 | SOTA multilingual, 100+ langs, 2.3 GB |
| `bge-small` | `BAAI/bge-small-en-v1.5` | 384 | Fast English, 67 MB |
| `bge-large` | `BAAI/bge-large-en-v1.5` | 1024 | Large English, 1.2 GB |
| `nomic-v1.5` | `nomic-ai/nomic-embed-text-v1.5` | 768 | Long-context EN, 8k tokens |
| `jina-v3` | `jinaai/jina-embeddings-v3` | 1024 | Multilingual 90+ langs, 8k tokens |
| `snowflake-xs` | `snowflake/snowflake-arctic-embed-xs` | 384 | Lightweight EN, 90 MB |

Models are auto-downloaded on first use via FastEmbed (ONNX runtime, no GPU required).

### Custom models

Use any FastEmbed-supported model by ID:

```yaml
embedding_model: "BAAI/bge-m3"
```
Or provide a custom HuggingFace model:

```yaml
embedding_model:
  id: "my-org/custom-embeddings"
  dimensions: 768
  pooling: mean           # mean | cls
  model_file: "onnx/model.onnx"
```
### Model migration

Switch embedding models on existing knowledge bases without re-ingesting:

```
→ rag.migrate_embeddings(knowledge_base="docs", target_model="bge-m3")
```

This re-embeds all documents in batches of 256 and invalidates the semantic cache.

---

## Vector Backends

Five backends, swappable via configuration:

| Backend | Type | Best for | Dependencies |
|---------|------|----------|-------------|
| **Qdrant** | Embedded / remote | Default, zero-config, quantization | `qdrant-client` (included) |
| **ChromaDB** | Embedded / remote | Simple local use | `chromadb` |
| **LanceDB** | Embedded (file-based) | Serverless, columnar | `lancedb`, `pyarrow` |
| **Pinecone** | Cloud | Managed, scalable | `pinecone` |
| **pgvector** | PostgreSQL extension | When Postgres already in stack | `asyncpg`, `pgvector` |

### Qdrant (default)

```yaml
backend:
  type: qdrant
  # In-memory (default):
  path: ""
  # Persistent:
  path: "/data/qdrant"
  # Remote server:
  url: "http://qdrant:6333"
  # Quantization (3x faster search, <1% recall loss):
  quantization: int8    # none | int8 | binary
```
### ChromaDB

```yaml
backend:
  type: chroma
  # In-memory:
  path: ""
  # Persistent:
  path: "/data/chroma"
  # Remote:
  url: "http://chroma:8000"
```
### LanceDB

```yaml
backend:
  type: lancedb
  path: "/data/lancedb"   # File-based, always persistent
```
### Pinecone

```yaml
backend:
  type: pinecone
  api_key: "{{env.PINECONE_API_KEY}}"
  index_name: "my-index"
  cloud: aws
  region: us-east-1
```
### pgvector

```yaml
backend:
  type: pgvector
  dsn: "postgres://user:pass@host:5432/mydb"
```
---

## Retrieval Strategies

### Hybrid (default)

Combines semantic search (vector similarity) with keyword search (BM25) using Reciprocal Rank Fusion:

```
Query → Embed → Vector Search (semantic)  ─┐
                                            ├─ RRF Fusion → Top-K results
Query → Tokenize → BM25 Search (keyword)  ─┘
```

The `bm25_weight` and `semantic_weight` control the fusion balance. Default (0.3 / 0.7) favors semantic understanding while keeping keyword precision.

```yaml
pipeline:
  retrieval: hybrid
  bm25_weight: 0.3
  semantic_weight: 0.7
```
### Semantic

Pure vector similarity search. Fastest, best for conceptual questions:

```yaml
pipeline:
  retrieval: semantic
```
### BM25

Pure keyword search. Best for exact term matching (error codes, identifiers):

```yaml
pipeline:
  retrieval: bm25
```
### Strategy override per query

Agents can override the default strategy per query:

```
→ rag.query(knowledge_base="docs", query="error ERR-4052", strategy="bm25")
→ rag.query(knowledge_base="docs", query="how does caching work?", strategy="semantic")
```

---

## Cross-Encoder Reranking

Reranking significantly improves precision by scoring each (query, document) pair with a cross-encoder model:

```yaml
reranker: true              # Use default: minilm-l6
# Or specify a model:
reranker: "bge-reranker-base"
```
Available reranker models:

| Shortcut | Model | Description |
|----------|-------|-------------|
| `minilm-l6` | `cross-encoder/ms-marco-MiniLM-L-6-v2` | Fast, lightweight **(default)** |
| `bge-reranker-base` | `BAAI/bge-reranker-base` | Balanced quality |
| `bge-reranker-v2` | `BAAI/bge-reranker-v2-m3` | Multilingual |
| `jina-reranker-v2` | `jinaai/jina-reranker-v2-base-multilingual` | Multilingual, 8k tokens |
| `jina-reranker-tiny` | `jinaai/jina-reranker-v1-turbo-en` | Ultra-fast English |

Pipeline with reranking:

```
Query → Retrieve top 20 candidates → Rerank all 20 with cross-encoder → Return top 5
```

```yaml
pipeline:
  rerank_top_n: 20    # Retrieve 20, rerank, return final_top_k
  final_top_k: 5
```
---

## Multi-Query Expansion

Generates multiple variants of the user's query via LLM, retrieves for each, and fuses results:

```yaml
pipeline:
  multi_query:
    enabled: true
    provider: my_llm       # LLM provider_id
    num_variants: 3
```
Flow:

```
"How does auth work?"
  → LLM generates: ["authentication mechanism", "login flow", "user verification"]
  → Retrieve for original + 3 variants (4 parallel searches)
  → RRF fusion across all result sets
  → Return top-K
```

If no LLM is configured, falls back to heuristic variants (word slicing, prefix/suffix extraction).

Action:

```
→ rag.multi_query(knowledge_base="docs", query="how does auth work?", num_variants=3)
```

---

## Semantic Cache

Caches retrieval results keyed by query embedding similarity. Hit rate: 15-40% in production.

```yaml
cache:
  enabled: true
  backend: memory           # memory | redis
  similarity_threshold: 0.95
  ttl: 3600                 # 1 hour
  max_entries: 10000
```
### How it works

```
Query arrives
  → Embed query (3-8ms)
  → Search cache by cosine similarity
  → HIT (similarity > 0.95): return cached results (<15ms total)
  → MISS: full retrieval pipeline (200ms-3s)
    → Store results + source hashes in cache
```

### Invalidation

Cache entries track the content hashes of their source documents. When a document changes (via watcher or re-ingestion), all cache entries that referenced it are automatically invalidated.

### Redis backend (multi-worker)

For production with multiple workers:

```yaml
cache:
  backend: redis
  redis_url: "redis://redis:6379/0"
```
---

## Citations

Every retrieval result carries source provenance. Citations are injected into the LLM context so responses cite their sources:

```yaml
citations:
  enabled: true
  format: inline
  verify: false    # Optional: check LLM output for invalid [N] references
```
### Context block format

The module formats retrieved results as a numbered context block for the LLM:

```
## Retrieved context — cite sources using [1], [2], etc.

[1] (source: docs/auth.md, section: Overview, confidence: 0.92)
Authentication uses JWT tokens with RSA-256 signing...

[2] (source: database:crm:users, query: "SELECT count(*) FROM users", confidence: 0.98)
| Total users | Active |
|-------------|--------|
| 12,450      | 11,203 |

[3] (source: policies/security.pdf, page 12, confidence: 0.87)
All API endpoints require a valid bearer token...
```

### Citation instruction

The LLM receives this instruction automatically:

> When answering, ALWAYS cite your sources using [N] notation. If sources conflict, mention both. If no source supports a claim, say "I don't have a source for this."

---

## Text2SQL

Answer natural language questions by generating and executing SQL against connected databases:

```yaml
modules:
  database:
    connections:
      crm:
        driver: postgresql
        host: db.internal
        database: crm

  rag:
    text2sql:
      enabled: true
      provider: main_brain
```
### How it works

```
"How many active customers signed up last quarter?"
  → Schema lookup (vector search on indexed DDL)
  → SQL generation (LLM with table schemas + few-shot examples)
  → SQL validation (SELECT only, blocks DDL/DML)
  → SQL execution (via database module)
  → Result formatting (markdown table + citation)
```

### Safety

The Text2SQL strategy **only allows SELECT queries**. All DML (`INSERT`, `UPDATE`, `DELETE`) and DDL (`CREATE`, `DROP`, `ALTER`, `TRUNCATE`, `GRANT`) are blocked before execution.

### SQL example cache

Validated (question, SQL) pairs are cached. When a similar question arrives:
1. Similarity > 0.95: reuse the cached SQL directly
2. Otherwise: cached pairs become few-shot examples for better generation

Action:

```
→ rag.sql_query(query="how many active users?", connection_id="crm")
```

---

## Corrective RAG (CRAG)

Evaluates retrieval quality and falls back to broader queries when results are poor:

```yaml
crag:
  enabled: true
  provider: my_llm
  confidence_threshold: 0.5
  fallback: broader_query    # broader_query | none
```
Flow:

```
Query → Retrieve → Score results
  → All results above threshold → return as-is
  → Some below threshold → filter out low-quality
  → All below / empty → try broader query fallback
```

Without an LLM provider, CRAG uses the retrieval score for quality filtering.

---

## Adaptive Routing

Automatically selects the best retrieval strategy based on query type:

```yaml
adaptive:
  enabled: true
  strategies:
    factual:
      retrieval: semantic
    analytical:
      retrieval: hybrid
      bm25_weight: 0.5
      semantic_weight: 0.5
```
The `QueryRouter` classifies queries using regex-based signal detection:

| Signal | Pattern examples | Route |
|--------|-----------------|-------|
| SQL | "how many", "total", "average", "count", "last quarter" | `sql` |
| Semantic | "what is", "explain", "how does", "policy on" | `semantic` |
| Hybrid | "compare", "difference between", "versus" | `hybrid` |

Classification runs in <5ms (no LLM call).

---

## Ingestion

### Supported formats

| Extension | Ingestor | Strategy |
|-----------|----------|----------|
| `.txt`, `.rst`, `.log` | PlainTextIngestor | Recursive chunking |
| `.md` | MarkdownIngestor | Split by headers (preserves hierarchy) |
| `.py`, `.ts`, `.js`, `.go`, `.rs`, `.java`, `.rb`, `.c`, `.cpp`, `.cs` | CodeIngestor | Language-aware blocks |
| `.csv` | CSVIngestor | One document per row |
| `.json` | JSONIngestor | Flatten objects/arrays |
| `.jsonl` | JSONLIngestor | One document per line |
| `.html`, `.htm` | HTMLIngestor | Strip tags, extract text |
| `.pdf` | PDFIngestor | Via `pdf` module (async) |
| `.xlsx`, `.xls` | SpreadsheetIngestor | Via `spreadsheet` module (async) |

### Incremental ingestion

The IndexingEngine tracks content hashes for every ingested file. Re-ingesting a file that hasn't changed is automatically skipped (no wasted embeddings compute).

### File ingestion

```
→ rag.ingest_file(knowledge_base="docs", path="./guide.md")
→ rag.ingest_directory(knowledge_base="docs", path="./docs", extensions=[".md", ".txt", ".pdf"])
```

### Text ingestion

```
→ rag.ingest(knowledge_base="docs", documents=["First doc text", "Second doc text"],
             source_type="manual", source_id="my-source")
```

### Database ingestion

Ingest database table schemas and/or row content:

```
→ rag.ingest_database(knowledge_base="crm_data", connection_id="crm",
                       tables={"users": {"columns": ["name", "bio"], "mode": "embed_rows"},
                               "orders": {"mode": "schema_only"}})
```

---

## Database Sources

### Table configuration

Each table must be explicitly declared with its columns (no automatic full-database indexing):

```yaml
sources:
  - type: database
    connection_id: crm
    tables:
      # Embed row content for search
      users:
        columns: [id, name, email, bio, department]
        mode: embed_rows
        template: "{name} ({department}) - {bio}"
        sync: updated_at
        max_rows: 50000

      # Index schema only (for Text2SQL)
      orders:
        mode: schema_only

      # Unlisted tables are completely ignored
```
### Two modes per table

| Mode | What is indexed | Sync | Use when |
|------|----------------|------|----------|
| `schema_only` | DDL + column descriptions + 5 sample rows | Schema changes only | Large tables, analytics, Text2SQL |
| `embed_rows` | Each row as a document (templated text) | Row-level sync | Tables with searchable text content |

### Row templates

For `embed_rows`, each row is converted to text before embedding. The default is column concatenation. With `template`, you control the format:

```yaml
users:
  columns: [name, department, bio]
  template: "{name} ({department}) - {bio}"
  # Produces: "Alice Martin (Engineering) - Backend specialist..."
```
---

## Database Sync

Three strategies for detecting row-level changes:

| Strategy | Mechanism | Latency | Prerequisites | Best for |
|----------|-----------|---------|--------------|----------|
| `updated_at` | `WHERE updated_at > watermark` | 30s | `updated_at` column + index | Most tables |
| `changelog` | Trigger-based changelog table | 30s | Auto-created triggers | Tables without `updated_at` |
| `notify` | PostgreSQL `LISTEN/NOTIFY` | <1s | PostgreSQL only | Near-real-time needs |

### updated_at (recommended)

Polls for rows modified since the last watermark. Requires an `updated_at` column with an index.

```yaml
sync:
  strategy: updated_at
  interval: 30    # seconds between polls
```
### changelog

The module auto-creates a `_rag_changelog` table and triggers on declared tables. All INSERT, UPDATE, DELETE operations are logged.

```yaml
sync:
  strategy: changelog
  auto_create_triggers: true
  prune_after_hours: 24
```
### notify (PostgreSQL)

Wraps `updated_at` or `changelog` with `LISTEN/NOTIFY` for near-instant change detection. Falls back to polling if the listener disconnects.

```yaml
sync:
  strategy: notify
  interval: 30    # Fallback polling interval
```
### Guarantees

- **Resumable**: watermarks are persisted in `state_snapshot()`. After restart, resumes from last position.
- **Idempotent**: double-processing is safe (upsert semantics).
- **Low overhead**: 1 indexed query per table per poll (~3 queries/second for 100 tables).

---

## Streaming Retrieval

The pipeline supports streaming retrieval for faster time-to-first-token:

```
Query → Launch semantic + BM25 in parallel
  → First results ready within 300ms → start LLM generation
  → Late results arrive → used for citation verification
```

This is used internally by the pipeline when both semantic and BM25 are active, and can reduce perceived latency significantly.

---

## Performance Targets

| Path | Target latency | How |
|------|---------------|-----|
| Cache hit | <15ms | Embed query (5ms) + cosine search (5ms) + return |
| Semantic search | <200ms + LLM | Embed (5ms) + ANN search (5ms) + rerank (100ms) |
| Hybrid search | <200ms + LLM | Parallel semantic + BM25, RRF fusion |
| Text2SQL | <500ms + LLM | Schema lookup + SQL gen + execution |
| Multi-query | <800ms + LLM | 4 parallel searches + RRF fusion |

Optimization techniques:
- **Local embeddings** (FastEmbed ONNX): 3-8ms, no API calls
- **Quantization** (Qdrant int8): 3x faster search, <1% recall loss
- **Semantic cache**: eliminates pipeline on 15-40% of queries
- **Streaming retrieval**: start LLM generation before all results arrive
- **Incremental indexing**: content hashing skips unchanged files

---

## Complete Examples

### Minimal — zero-config RAG

```yaml
app:
  app_id: rag-simple
  name: "Simple RAG"

modules:
  rag: {}

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: "You answer questions using the RAG knowledge base."

capabilities:
  default_policy: auto
  grant:
    - module: rag
```
### Documentation assistant

```yaml
app:
  app_id: docs-assistant
  name: "Documentation Assistant"

variables:
  workspace: ./docs

modules:
  rag:
    embedding_model: bge-small
    reranker: true
    sources:
      - type: file
        path: "{{workspace}}"
        extensions: [.md, .txt, .pdf]
        watch: true
    pipeline:
      retrieval: hybrid
      rerank_top_n: 20
      final_top_k: 5
    cache:
      enabled: true
      ttl: 1800
    citations:
      enabled: true
      verify: true

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: "You answer questions using the RAG knowledge base."

capabilities:
  default_policy: auto
  grant:
    - module: rag
```
### Enterprise multi-source (database + documents)

```yaml
app:
  app_id: enterprise-rag
  name: "Enterprise Knowledge"

modules:
  database:
    connections:
      crm:
        driver: postgresql
        host: db.internal
        database: crm
        credentials: "{{env.DB_CREDENTIALS}}"
        policy: { preset: readonly }

  rag:
    embedding_model: bge-m3
    reranker: true
    backend:
      type: qdrant
      path: /data/qdrant
      quantization: int8
    sources:
      - type: database
        connection_id: crm
        sync:
          strategy: updated_at
          interval: 30
        tables:
          users:
            columns: [id, name, email, bio, department]
            mode: embed_rows
            template: "{name} ({department}) - {bio}"
          products:
            columns: [id, name, description, category]
            mode: embed_rows
          orders:
            mode: schema_only
          invoices:
            mode: schema_only
      - type: file
        path: "{{workspace}}/docs"
        extensions: [.md, .txt, .pdf]
        watch: true
      - type: file
        path: "{{workspace}}/policies"
        extensions: [.pdf]
        watch: true
    pipeline:
      retrieval: hybrid
      multi_query:
        enabled: true
        provider: enrichment
        num_variants: 3
      rerank_top_n: 30
      final_top_k: 5
    text2sql:
      enabled: true
      provider: enrichment
    cache:
      enabled: true
      ttl: 1800
    citations:
      enabled: true
      format: inline
      verify: true

  llm_provider:
    providers:
      enrichment:
        backend: openai_compat
        model: gpt-4o-mini
        api_key: "{{env.OPENAI_API_KEY}}"

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: "You answer enterprise knowledge questions using RAG + CRM DB."

capabilities:
  default_policy: auto
  grant:
    - module: rag
    - module: database
      actions: [fetch_results]
```
### Database analytics (no documents)

```yaml
app:
  app_id: analytics-rag
  name: "Analytics Assistant"

modules:
  database:
    connections:
      warehouse:
        driver: postgresql
        host: analytics.internal
        database: warehouse

  rag:
    sources:
      - type: database
        connection_id: warehouse
        sync:
          strategy: changelog
          auto_create_triggers: true
        tables:
          customers:
            columns: [id, name, segment, lifetime_value]
            mode: embed_rows
            template: "Customer: {name}, segment {segment}, LTV ${lifetime_value}"
          products:
            columns: [id, name, description]
            mode: embed_rows
          orders:
            mode: schema_only
          revenue:
            mode: schema_only
    text2sql:
      enabled: true
      provider: main_brain
      example_cache: true

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: "You answer questions using the RAG knowledge base."

capabilities:
  default_policy: auto
  grant:
    - module: rag
```
---

## Relationship with other modules

| Module | Relationship |
|--------|-------------|
| `vector` | Independent. `rag` has its own backend abstraction. Use `vector` for simple vector ops, `rag` for full RAG pipelines. |
| `database` | `rag` calls `database` via ServiceBus for Text2SQL execution, schema introspection, and row fetching. |
| `pdf` | `rag` calls `pdf.read` via ServiceBus for PDF ingestion. |
| `spreadsheet` | `rag` calls `spreadsheet.read` via ServiceBus for Excel ingestion. |
| `context_builder` | Shares the FastEmbed singleton when using `minilm-l12` (no duplicate model loading). |
| `index` | Independent. `rag` has its own indexing engine with content hashing. |

---

## State Persistence

The module persists its state via `state_snapshot()` / `restore_state()`:

- Knowledge base metadata (names, descriptions, models, counts)
- BM25 indexes (serialized term frequencies)
- Content hashes (for incremental ingestion)
- Cache statistics (hit rate, entries, evictions)
- Database sync watermarks (resume position)

The vector backend data is persisted independently by the backend itself (Qdrant on disk, LanceDB files, etc.).
