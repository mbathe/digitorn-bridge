---
id: rag
title: RAG Module
sidebar_label: RAG
sidebar_position: 37
description: Retrieval-Augmented Generation - knowledge bases, hybrid search, citations, semantic cache, Text2SQL.
---

# RAG Module

The `rag` module is a production-grade Retrieval-Augmented
Generation engine. It unifies files, databases, and free-text
sources into named **knowledge bases** with hybrid retrieval
(BM25 + semantic), cross-encoder reranking, source citations,
semantic cache, and an optional Text2SQL strategy.

Every claim on this page maps to real code under
`packages/digitorn/modules/rag/`. Entries are cited with file +
line.

## Why a dedicated RAG module?

The existing `vector` module exposes basic vector ops (add,
search, delete) on raw collections. `rag` builds on top with:

- **Knowledge bases** — named, versioned collections that pair
  a vector index with a parallel BM25 index and per-source
  metadata.
- **Hybrid retrieval** — Reciprocal Rank Fusion (RRF) of BM25
  + semantic by default.
- **Cross-encoder reranking** for precision (`reranker.py`).
- **Source citations** injected directly into the LLM context.
- **Semantic cache** (`cache.py`) for sub-15 ms repeated queries.
- **Multi-format ingestion** (Markdown, PDF, code, CSV, JSON,
  HTML, databases) — `indexing/ingestors.py`.
- **Database sync** (`indexing/sync.py`) — `updated_at`,
  changelog triggers, or LISTEN/NOTIFY.
- **Text2SQL** (`strategies/text2sql.py`) for natural-language
  questions over structured data.
- **Multi-query expansion** for broader recall
  (`strategies/multiquery.py`).
- **Corrective RAG** with quality-evaluation fallback
  (`strategies/crag.py`).
- **6 vector backends** — Qdrant, ChromaDB, LanceDB, Pinecone,
  pgvector, Elasticsearch.

## Zero-config quick start

```yaml
tools:
  modules:
    rag: {}
```

| Setting | Default | Source |
|---------|---------|--------|
| Embedding model | `minilm-l12` (384 d, multilingual, 220 MB) | `embeddings.py` |
| Vector backend | Qdrant in-memory | `config.py` |
| Retrieval strategy | Hybrid (BM25 + semantic + RRF) | `config.py` |
| Chunking | `recursive`, 500 chars, 50 overlap | `config.py` |
| Semantic cache | Enabled, in-memory, 1 h TTL | `config.py` |
| Citations | Enabled, inline | `config.py` |
| Reranker | Disabled | `module.py` |

The agent then creates KBs and ingests via tool calls:

```
Agent: I'll create a knowledge base and index your docs.
  → rag.create_knowledge_base(name="docs")
  → rag.ingest_directory(knowledge_base="docs", path="./docs",
                          extensions=[".md", ".txt"])
  → rag.query(knowledge_base="docs", query="how does authentication work?")
```

## The 14 actions

`@action` decorators in `module.py` (line numbers cited):

| Tool | Source | Purpose |
|------|--------|---------|
| `rag.create_knowledge_base` | `:545` | Create a named KB. |
| `rag.delete_knowledge_base` | `:581` | Drop a KB + its vector + BM25 indexes. |
| `rag.list_knowledge_bases` | `:599` | Enumerate KBs with metadata. |
| `rag.knowledge_base_stats` | `:618` | Counts, model, last sync, hit rate. |
| `rag.ingest` | `:661` | Add raw text documents. |
| `rag.ingest_file` | `:711` | Add a single file. |
| `rag.ingest_directory` | `:1022` | Walk a directory + add matching files. |
| `rag.ingest_database` | `:1101` | Index DB tables (rows or schema-only). |
| `rag.query` | `:805` | Retrieve from a KB (default strategy or override). |
| `rag.multi_query` | `:904` | LLM-expanded query with RRF fusion. |
| `rag.sql_query` | `:970` | Text2SQL — generate + run a SELECT. |
| `rag.clear_cache` | `:1175` | Wipe the semantic cache. |
| `rag.migrate_embeddings` | `:1198` | Switch a KB to a new embedding model (re-embeds in batches). |
| `rag.list_models` | `:1270` | List available embedding + reranker shortcuts. |

## Configuration reference

`RagConfig` (`config.py`). Mounted under
`tools.modules.rag.config:` (the `config:` wrapper is
mandatory — see [App Configuration](02-app-config.md)).

```yaml
tools:
  modules:
    rag:
      config:
        embedding_model: minilm-l12
        reranker: false                # true | "<shortcut>" | "<HF id>"
        backend:
          type: qdrant                  # qdrant | chroma | lancedb | pinecone | pgvector | elasticsearch
          path: ""
          url: ""
          quantization: none            # none | int8 | binary (qdrant only)
        pipeline:
          retrieval: hybrid             # hybrid | semantic | bm25
          bm25_weight: 0.3
          semantic_weight: 0.7
          rerank_top_n: 20              # 0 = skip rerank
          final_top_k: 5
          multi_query:
            enabled: false
            provider: ""                # llm_provider id for query expansion
            num_variants: 3             # 2..10
        chunking:
          strategy: recursive           # fixed | sentence | paragraph | recursive
          size: 500                     # 50..10000
          overlap: 50                   # 0..500
        sources:
          - type: file
            path: "{{workspace}}/docs"
            extensions: [.md, .txt, .pdf]
            watch: true
            recursive: true
            max_files: 1000
          - type: database
            connection_id: crm
            sync:
              strategy: updated_at      # updated_at | changelog | notify
              interval: 30
              auto_create_triggers: true
              prune_after_hours: 24
            tables:
              users:
                columns: [id, name, email, bio, department]
                mode: embed_rows        # schema_only | embed_rows
                template: "{name} ({department}) - {bio}"
                sync: updated_at
                max_rows: 50000
              orders:
                mode: schema_only
        auto_index:
          on_start: true
          schedule: ""                  # cron expr (uses cron_native)
        cache:
          enabled: true
          backend: memory               # memory | redis
          similarity_threshold: 0.95    # 0.80..1.0
          ttl: 3600
          max_entries: 10000
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
          provider: ""
          confidence_threshold: 0.5
          fallback: broader_query       # broader_query | none
        adaptive:
          enabled: false
          provider: ""
          strategies: {}
        contextual_retrieval:
          enabled: false
          provider: ""
          concurrency: 5                # 1..20
          prompt_template: ""
        max_knowledge_bases: 50
        max_documents: 100000
        persistence_dir: ""
```

## Embedding models

`BUILTIN_MODELS` at `embeddings.py`. **7 built-ins**,
auto-downloaded by FastEmbed (ONNX, no GPU needed):

| Shortcut | FastEmbed id | Dims | Notes |
|----------|--------------|------|-------|
| `minilm-l12` *(default)* | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 384 | Multilingual, 50 langs, 220 MB. |
| `bge-m3` | `BAAI/bge-m3` | 1024 | SOTA multilingual, 100+ langs, 2.3 GB. |
| `bge-small` | `BAAI/bge-small-en-v1.5` | 384 | Fast English, 67 MB. |
| `bge-large` | `BAAI/bge-large-en-v1.5` | 1024 | Large English, 1.2 GB. |
| `nomic-v1.5` | `nomic-ai/nomic-embed-text-v1.5` | 768 | Long-context EN, 8 k tokens. |
| `jina-v3` | `jinaai/jina-embeddings-v3` | 1024 | Multilingual 90+ langs, 8 k tokens. |
| `snowflake-xs` | `snowflake/snowflake-arctic-embed-xs` | 384 | Lightweight EN, 90 MB. |

### Custom models

Use any FastEmbed-supported HuggingFace id directly:

```yaml
config:
  embedding_model: "BAAI/bge-m3"
```

Or supply a full custom spec:

```yaml
config:
  embedding_model:
    id: "my-org/custom-embeddings"
    dimensions: 768
    pooling: mean              # mean | cls
    model_file: "onnx/model.onnx"
```

### Live model migration

`rag.migrate_embeddings(knowledge_base="docs", target_model="bge-m3")`
re-embeds all documents in batches and invalidates the
semantic cache. No KB downtime.

## Reranker models

`BUILTIN_RERANKERS` at `reranker.py`. **5 built-ins**;
default `minilm-l6` (`reranker.py`):

| Shortcut | HF id | Notes |
|----------|-------|-------|
| `minilm-l6` *(default)* | `Xenova/ms-marco-MiniLM-L-6-v2` | Fast, lightweight. |
| `minilm-l12` | `Xenova/ms-marco-MiniLM-L-12-v2` | Larger ms-marco model. |
| `bge-reranker-base` | `BAAI/bge-reranker-base` | Balanced quality. |
| `jina-reranker-v1-tiny` | `jinaai/jina-reranker-v1-tiny-en` | Ultra-fast English. |
| `jina-reranker-v2` | `jinaai/jina-reranker-v2-base-multilingual` | Multilingual, 8 k tokens. |

```yaml
config:
  reranker: true                # use the default minilm-l6
  # OR
  reranker: "bge-reranker-base"
```

Pipeline with rerank:

```
query → retrieve top rerank_top_n → cross-encoder score → keep final_top_k
```

## Vector backends

`BackendConfig.type` (`config.py`). 6 backends, swappable
in YAML:

| Backend | Mode | Best for | Pip dep |
|---------|------|----------|---------|
| **Qdrant** | Embedded / remote | Default, zero-config, quantization. | `qdrant-client` (bundled). |
| **ChromaDB** | Embedded / remote | Simple local use. | `chromadb`. |
| **LanceDB** | Embedded (file) | Serverless, columnar. | `lancedb`, `pyarrow`. |
| **Pinecone** | Cloud | Managed, scalable. | `pinecone`. |
| **pgvector** | PostgreSQL ext. | When Postgres is already in the stack. | `asyncpg`, `pgvector`. |
| **Elasticsearch** | Remote cluster | Existing ES cluster + lexical filters. | `elasticsearch`. |

### Qdrant (default)

```yaml
backend:
  type: qdrant
  path: ""                  # "" = in-memory (default)
  # path: /data/qdrant      # persistent on-disk
  # url: http://qdrant:6333 # remote
  quantization: int8        # none | int8 | binary
```

`int8` quantization gives ~3× faster search at <1% recall loss.

### ChromaDB

```yaml
backend:
  type: chroma
  path: /data/chroma          # "" = in-memory
  # url: http://chroma:8000   # remote
```

### LanceDB

```yaml
backend:
  type: lancedb
  path: /data/lancedb         # always file-based
```

### Pinecone

```yaml
backend:
  type: pinecone
  api_key: "{{secret.PINECONE_API_KEY}}"
  index_name: my-index
  cloud: aws
  region: us-east-1
```

### pgvector

```yaml
backend:
  type: pgvector
  dsn: "postgres://user:pass@host:5432/mydb"
```

### Elasticsearch

```yaml
backend:
  type: elasticsearch
  url: http://es:9200
  # api_key, username/password supported via the ES client config
```

## Retrieval strategies

`PipelineConfig.retrieval` (`config.py`).

### Hybrid (default)

Semantic + BM25 fused via Reciprocal Rank Fusion. The
`bm25_weight` (default 0.3) and `semantic_weight` (default 0.7)
control balance.

```yaml
config:
  pipeline:
    retrieval: hybrid
    bm25_weight: 0.3
    semantic_weight: 0.7
```

### Semantic

Pure vector similarity. Fastest, best for conceptual questions.

### BM25

Pure keyword. Best for exact-match queries (error codes,
identifiers).

### Per-call override

```
rag.query(knowledge_base="docs", query="error ERR-4052", strategy="bm25")
rag.query(knowledge_base="docs", query="how does caching work?", strategy="semantic")
```

## Multi-query expansion

`MultiQueryConfig` (`config.py`).
`strategies/multiquery.py`.

```yaml
config:
  pipeline:
    multi_query:
      enabled: true
      provider: enrichment       # llm_provider id
      num_variants: 3            # 2..10
```

Flow:

```
"how does auth work?"
  → LLM emits ["authentication mechanism", "login flow", "user verification"]
  → 4 parallel searches (original + 3 variants)
  → RRF fusion → top-K
```

When no LLM provider is configured, falls back to heuristic
variants (word slicing, prefix / suffix). Action:
`rag.multi_query`.

## Semantic cache

`cache.py`. Cached by query embedding similarity. Reported
hit rate 15-40 % in production.

```yaml
config:
  cache:
    enabled: true
    backend: memory             # memory | redis
    similarity_threshold: 0.95  # 0.80..1.0
    ttl: 3600
    max_entries: 10000
    redis_url: "redis://redis:6379/0"   # required when backend=redis
```

How it works:

```
query → embed (3-8 ms) → cosine search in cache
        HIT  (similarity > threshold) → return cached results (<15 ms)
        MISS → full pipeline (200 ms-3 s) → store with source hashes
```

Each cache entry records the content hashes of its source
documents. When a document is re-ingested, every cache entry
that referenced it is invalidated automatically.

## Citations

`CitationConfig` (`config.py`).

```yaml
config:
  citations:
    enabled: true
    format: inline              # inline | footnote | structured
    verify: false               # check LLM output for invalid [N] refs
```

The module formats retrieved results as a numbered context
block injected into the LLM input:

```
## Retrieved context — cite sources using [1], [2], etc.

[1] (source: docs/auth.md, section: Overview, confidence: 0.92)
Authentication uses JWT tokens with RSA-256 signing...

[2] (source: database:crm:users, query: "SELECT count(*) FROM users", confidence: 0.98)
| Total users | Active |
|-------------|--------|
| 12 450      | 11 203 |

[3] (source: policies/security.pdf, page 12, confidence: 0.87)
All API endpoints require a valid bearer token...
```

The LLM also receives a citation instruction:

> When answering, ALWAYS cite your sources using `[N]` notation.
> If sources conflict, mention both. If no source supports a
> claim, say "I don't have a source for this."

When `verify: true`, the citation post-processor flags
references like `[7]` that don't appear in the context block.

## Text2SQL

`Text2SQLConfig` (`config.py`).
`strategies/text2sql.py`.

```yaml
tools:
  modules:
    database:
      config:
        connections:
          crm:
            driver: postgresql
            host: db.internal
            database: crm

    rag:
      config:
        text2sql:
          enabled: true
          provider: enrichment      # llm_provider id
          example_cache: true
```

How it works:

```
"How many active customers signed up last quarter?"
  → schema lookup (vector search on indexed DDL)
  → LLM SQL generation with table schemas + few-shot examples
  → SQL validation (SELECT-only allowlist)
  → execute via the database module
  → format result (markdown table) + citation
```

### Safety

The strategy **only allows SELECT**. All DML (`INSERT`,
`UPDATE`, `DELETE`) and DDL (`CREATE`, `DROP`, `ALTER`,
`TRUNCATE`, `GRANT`) are blocked before execution.

### Example cache

When `example_cache: true`, validated `(question, SQL)` pairs
are cached. New questions:

1. similarity > 0.95 → reuse cached SQL directly;
2. otherwise → cached pairs become few-shot examples for
   better generation.

Action: `rag.sql_query(query="how many active users?", connection_id="crm")`.

## Corrective RAG (CRAG)

`CragConfig` (`config.py`).
`strategies/crag.py`.

```yaml
config:
  crag:
    enabled: true
    provider: enrichment
    confidence_threshold: 0.5
    fallback: broader_query       # broader_query | none
```

```
query → retrieve → score
  all results above threshold → return as-is
  some below threshold        → drop low-quality
  all below / empty           → run broader_query fallback
```

Without an LLM provider, CRAG uses the raw retrieval score
for filtering.

## Adaptive routing

`AdaptiveConfig` (`config.py`).
`strategies/adaptive.py`. Picks a strategy per query type:

```yaml
config:
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

The query router (`router.py`) classifies queries via regex
signal detection (<5 ms, no LLM call):

| Signal | Pattern examples | Default route |
|--------|------------------|---------------|
| SQL | `how many`, `total`, `average`, `count`, `last quarter` | `sql` |
| Semantic | `what is`, `explain`, `how does`, `policy on` | `semantic` |
| Hybrid | `compare`, `difference between`, `versus` | `hybrid` |

## Contextual retrieval

`ContextualRetrievalConfig` (`config.py`).
`strategies/contextual.py`. Pre-generates a per-chunk context
sentence (Anthropic-style "Contextual Retrieval") before
embedding. Improves recall on long documents at ingest cost.

```yaml
config:
  contextual_retrieval:
    enabled: true
    provider: enrichment
    concurrency: 5                # 1..20
    prompt_template: |
      <document>{document}</document>
      <chunk>{chunk}</chunk>
      Provide a short context anchoring this chunk in the document.
```

## Ingestion

`indexing/ingestors.py`. Detects extension, picks an ingestor,
chunks per the strategy, embeds, indexes BM25 + vector.

| Extension | Ingestor | Strategy |
|-----------|----------|----------|
| `.txt`, `.rst`, `.log` | PlainText | Recursive chunking. |
| `.md` | Markdown | Split by headers (preserves hierarchy). |
| `.py`, `.ts`, `.js`, `.go`, `.rs`, `.java`, `.rb`, `.c`, `.cpp`, `.cs` | Code | Language-aware blocks. |
| `.csv` | CSV | One document per row. |
| `.json` | JSON | Flatten objects / arrays. |
| `.jsonl` | JSONL | One document per line. |
| `.html`, `.htm` | HTML | Strip tags, extract text. |
| `.pdf` | PDF | Via `pdf` module (async). |
| `.xlsx`, `.xls` | Spreadsheet | Via `spreadsheet` module (async). |

### Incremental ingestion

The IndexingEngine (`indexing/engine.py`) tracks content hashes
per file. Re-ingesting an unchanged file is a no-op — no wasted
embedding compute.

### Ingest actions

```
rag.ingest_file(knowledge_base="docs", path="./guide.md")

rag.ingest_directory(
  knowledge_base="docs",
  path="./docs",
  extensions=[".md", ".txt", ".pdf"]
)

rag.ingest(
  knowledge_base="docs",
  documents=["First doc text", "Second doc text"],
  source_type="manual",
  source_id="my-source"
)

rag.ingest_database(
  knowledge_base="crm_data",
  connection_id="crm",
  tables={
    "users":  {"columns": ["name", "bio"], "mode": "embed_rows"},
    "orders": {"mode": "schema_only"}
  }
)
```

## Database sources

`DatabaseSourceConfig` (`config.py`),
`TableConfig` (`config.py`).

### Per-table modes

| `mode` | What is indexed | Sync | Use when |
|--------|-----------------|------|----------|
| `schema_only` | DDL + column descriptions + 5 sample rows. | Schema changes only. | Large tables, analytics, Text2SQL. |
| `embed_rows` | Each row as a document (templated text). | Row-level sync. | Tables with searchable text content. |

```yaml
sources:
  - type: database
    connection_id: crm
    tables:
      users:
        columns: [id, name, email, bio, department]
        mode: embed_rows
        template: "{name} ({department}) - {bio}"
        sync: updated_at
        max_rows: 50000

      orders:
        mode: schema_only
      # unlisted tables are completely ignored
```

For `embed_rows`, the row is rendered through the `template`
(default = column concatenation) before embedding.

## Database sync

`indexing/sync.py`. Three strategies:

| Strategy | Mechanism | Latency | Prerequisites | Best for |
|----------|-----------|---------|---------------|----------|
| `updated_at` | `WHERE updated_at > watermark` | 30 s (configurable) | `updated_at` column + index. | Most tables. |
| `changelog` | Trigger-based `_rag_changelog` table | 30 s | Auto-created triggers. | Tables without `updated_at`. |
| `notify` | PostgreSQL `LISTEN/NOTIFY` | <1 s | PostgreSQL only. | Near-real-time needs. |

```yaml
sync:
  strategy: updated_at
  interval: 30                 # poll interval (seconds)
```

```yaml
sync:
  strategy: changelog
  auto_create_triggers: true
  prune_after_hours: 24
```

```yaml
sync:
  strategy: notify
  interval: 30                 # fallback polling on listener disconnect
```

### Guarantees

- **Resumable** — watermarks live in `state_snapshot()`; after
  restart, sync resumes at the last position.
- **Idempotent** — double-processing is safe (upsert
  semantics).
- **Low overhead** — 1 indexed query per table per poll
  (~3 q/s for 100 tables at the default 30 s interval).

## Streaming retrieval

When both BM25 and semantic legs are active, the pipeline
launches them in parallel and starts the LLM as soon as the
first batch returns:

```
query → kick off semantic + BM25 in parallel
   → first results within ~300 ms → start LLM generation
   → late results arrive → used for citation verification
```

Reduces perceived latency on long-context generations.

## Performance targets

| Path | Target | How |
|------|--------|-----|
| Cache hit | <15 ms | Embed query (5 ms) + cosine search (5 ms). |
| Semantic search | <200 ms + LLM | Embed (5 ms) + ANN search (5 ms) + rerank (~100 ms). |
| Hybrid search | <200 ms + LLM | Parallel semantic + BM25, RRF fusion. |
| Text2SQL | <500 ms + LLM | Schema lookup + SQL gen + execute. |
| Multi-query | <800 ms + LLM | 4 parallel searches + fusion. |

Optimisation levers:

- **Local embeddings** (FastEmbed ONNX) — 3-8 ms, no API calls.
- **Quantization** (Qdrant `int8`) — ~3× faster, <1% recall loss.
- **Semantic cache** — eliminates pipeline on 15-40% of queries.
- **Streaming retrieval** — start LLM before all results.
- **Incremental indexing** — content hashing skips unchanged files.

## Shared instance, per-app reconfig

The rag module has `isolation = "shared"` (one instance per
daemon — many apps see the same backend storage). Its
`on_start()` runs **once** at daemon boot with whatever empty
config the module has at that moment → default in-memory
backend.

When an app is activated, the bootstrap calls
`module.on_config_update(cfg)` with that app's config. The
overridden `on_config_update` (in
`packages/digitorn/modules/rag/module.py`):

1. Compares old vs new backend path.
2. Closes the old backend if changed.
3. Re-creates + initialises the new backend with the new
   path.
4. Calls `_discover_existing_collections()` to rebuild `_kbs`
   from collections already on disk (populated by previous
   sessions or offline tools).

This is the only `shared` module that mutates its backend on
per-app activation.

> **Common config bug**: under `tools.modules.rag`, the
> backend block MUST live under `config:`. Without the
> wrapper, `compiled.modules["rag"].config = {}`, the
> bootstrap sees `if config:` as False, and never calls
> `on_config_update`. The result: every query returns
> "knowledge base not found". See
> [App Configuration → modules](02-app-config.md) for the
> general rule.

## Complete examples

### Minimal — zero-config RAG

```yaml
app:
  app_id: rag-simple
  name: Simple RAG

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: https://api.deepseek.com/v1
    system_prompt: You answer questions using the RAG knowledge base.

tools:
  modules:
    rag: {}
  capabilities:
    default_policy: auto
    grant:
      - {module: rag}
```

### Documentation assistant

```yaml
tools:
  modules:
    rag:
      config:
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
  capabilities:
    default_policy: auto
    grant:
      - {module: rag}

dev:
  variables:
    workspace: ./docs
```

### Enterprise multi-source (DB + documents)

```yaml
tools:
  modules:
    database:
      config:
        auto_connect:
          - connection_id: crm
            driver: postgresql
            host: db.internal
            database: crm

    rag:
      config:
        embedding_model: bge-m3
        reranker: true
        backend:
          type: qdrant
          path: /data/qdrant
          quantization: int8

        sources:
          - type: database
            connection_id: crm
            sync: {strategy: updated_at, interval: 30}
            tables:
              users:
                columns: [id, name, email, bio, department]
                mode: embed_rows
                template: "{name} ({department}) - {bio}"
              products:
                columns: [id, name, description, category]
                mode: embed_rows
              orders:   {mode: schema_only}
              invoices: {mode: schema_only}

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
          multi_query: {enabled: true, provider: enrichment, num_variants: 3}
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
      config:
        providers:
          enrichment:
            backend: openai_compat
            model: gpt-4o-mini
            api_key: "{{secret.OPENAI_API_KEY}}"

  capabilities:
    default_policy: auto
    grant:
      - {module: rag}
      - {module: database, actions: [fetch_results]}
```

### Database analytics (no documents)

```yaml
tools:
  modules:
    database:
      config:
        auto_connect:
          - connection_id: warehouse
            driver: postgresql
            host: analytics.internal
            database: warehouse

    rag:
      config:
        sources:
          - type: database
            connection_id: warehouse
            sync: {strategy: changelog, auto_create_triggers: true}
            tables:
              customers:
                columns: [id, name, segment, lifetime_value]
                mode: embed_rows
                template: "Customer: {name}, segment {segment}, LTV ${lifetime_value}"
              products:
                columns: [id, name, description]
                mode: embed_rows
              orders:  {mode: schema_only}
              revenue: {mode: schema_only}

        text2sql:
          enabled: true
          provider: main_brain
          example_cache: true

  capabilities:
    default_policy: auto
    grant:
      - {module: rag}
```

## Relationship with other modules

| Module | Relationship |
|--------|--------------|
| `vector` | Independent. Use `vector` for raw vector ops, `rag` for full pipelines. |
| `database` | `rag` calls `database` via the ServiceBus for Text2SQL execution + schema introspection + row fetching. |
| `pdf` | `rag` calls `pdf.read` via the ServiceBus for PDF ingestion. |
| `spreadsheet` | `rag` calls `spreadsheet.read` via the ServiceBus for Excel ingestion. |
| `context_builder` | Shares the FastEmbed singleton when both use `minilm-l12` (no duplicate model load). |
| `index` | Independent. The RAG indexing engine has its own content-hashing layer. |

## State persistence

`module.py` `state_snapshot()` /
`module.py` `restore_state()` persist:

- KB metadata (names, descriptions, models, doc counts).
- BM25 indexes (serialised term frequencies).
- Content hashes (incremental ingestion).
- Cache statistics (hit rate, entries, evictions).
- Database sync watermarks (resume position).

Vector backend data is persisted independently by the backend
itself (Qdrant on disk, LanceDB files, ChromaDB SQLite, etc.).

## Cross-references

- App-config block reference (`tools.modules.rag.config:`):
  [App Configuration → tools.modules](02-app-config.md#toolsmodules--module-config)
- Per-module reference (storage backend, advanced knobs):
  [modules/reference/rag.md](../reference/modules/rag.md)
- Credentials master / per-user keys for DBs and Pinecone:
  [credentials.md](../reference/runtime/credentials.md)
- Bundle namespaces (where `{{prompt.X}}` resolves):
  [Bundle namespaces](38-bundle-namespaces.md)
