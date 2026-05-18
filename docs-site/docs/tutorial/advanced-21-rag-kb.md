---
id: advanced-21-rag-kb
title: "Advanced 21 - RAG knowledge base query"
sidebar_label: "Advanced 21: RAG"
---

The `rag` module ships a complete retrieval-augmented
generation pipeline: pluggable vector backend (Qdrant,
Chroma, LanceDB, Pinecone, pgvector, Elasticsearch),
FastEmbed embeddings, hybrid search (semantic + BM25),
chunking, cache, and citations. This tutorial walks the
agent through the **bootstrap → ingest → query → answer
with citations** loop, all live.

Live-tested end-to-end: app `tuto-rag-kb`, session
`test-fee8063b`, brain `openai/gpt-5-mini`. Ingest
3 Markdown files (10 chunks), query for "hooks Digitorn",
agent returns 5 facts, each citing the source file path.

## The YAML

```yaml
app:
  app_id: tuto-rag-kb
  name: Tuto - RAG Knowledge Base Query
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: none
  max_turns: 10
  timeout: 180
  tool_injection: direct
  direct_modules: [rag]

agents:
  - id: main
    role: assistant
    brain:
      provider: openai
      backend: openai_compat
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      temperature: 0.1
      max_tokens: 4096
    system_prompt: |
      You are a Digitorn documentation assistant.

      First-turn bootstrap (only once per session):
      1. Call RagListKnowledgeBases. If the result is empty,
         do the FULL bootstrap below. The order matters:
         a. Call RagCreateKnowledgeBase with name="default".
         b. Call RagIngestDirectory with
            knowledge_base="default",
            path="C:/tmp/digitorn-tutorials/rag-kb",
            extensions=[".md"].
         Wait for it to finish (it returns the chunk count).
         RagIngestDirectory FAILS with "Knowledge base
         'default' not found" if you skip step (a).

      Then, on EVERY user question:
      1. Call RagQuery with knowledge_base="default",
         query=<the user question rephrased as a search>,
         top_k=3.
      2. Answer based on what RagQuery returned. Cite the
         source file path for each fact. If the result list
         is empty, say so explicitly instead of guessing.

tools:
  modules:
    rag:
      config:
        embedding_model: minilm-l12
        backend:
          type: qdrant
          # Empty path = in-memory (no disk persistence).
          # Sufficient for a tutorial; production would set
          # a path under the workspace for durability.
          path: ""
        sources:
          - type: file
            path: "C:/tmp/digitorn-tutorials/rag-kb"
            extensions: [".md"]
            recursive: false
        auto_index:
          on_start: true
        max_documents: 1000
  capabilities:
    default_policy: auto
    max_risk_level: low
    grant:
      - module: rag
        actions:
          - query
          - list_knowledge_bases
          - knowledge_base_stats
          - ingest_directory
          - create_knowledge_base
```

Four pitfalls to know:

- **`config:` wrapper is mandatory.** The rag module's
  schema is `extra: forbid` on top-level fields; anything
  under `rag:` that is not `config: ...`, `setup:`,
  `constraints:`, or `middleware:` is silently dropped.
  See [the CLAUDE.md memo](https://github.com/digitorn-ai/digitorn-bridge/blob/main/CLAUDE.md#module-config-yaml-structure-config-wrapper-required)
  for the full list.
- **`auto_index.on_start: true` does NOT run for shared
  modules.** The rag module is a shared singleton: its
  `on_start()` runs once at daemon boot, before any app
  exists. Per-app `sources:` only land via
  `on_config_update`, and the override
  `_discover_existing_collections()` only rebuilds the
  in-memory KB map from collections already on disk. It
  does not re-run the ingest. So the `sources:` and
  `auto_index:` blocks above are forward-looking — the
  agent has to ingest itself (next section), or you
  populate the KB offline via
  [`knowledge_base/build.py`](https://github.com/digitorn-ai/digitorn-bridge/blob/main/packages/digitorn/knowledge_base/build.py).
- **Order matters in the bootstrap.**
  `RagIngestDirectory` requires the KB to exist already.
  Skipping `RagCreateKnowledgeBase` first fails with
  `Knowledge base 'default' not found. Create it first.`
- **Grant the right actions.** Agents need
  `create_knowledge_base` + `ingest_directory` in
  addition to `query`. The default direct-tools build only
  exposes what `capabilities.grant` lists.

## Sample knowledge base

Three small Markdown files under
`C:/tmp/digitorn-tutorials/rag-kb/`:

- `hooks.md`: how the Hooks V2 engine works, the list of
  events, condition types, action types.
- `sub-agents.md`: the 8 invocation modes of the Agent
  tool, what `role: coordinator` and `role: specialist`
  do.
- `modules.md`: the module system, shared vs per-app
  instances, direct vs discovery injection modes.

## Deploy and run

```bash
digitorn dev deploy tuto-rag-kb.yaml
digitorn dev chat tuto-rag-kb -m 'EXACT bootstrap then question. Follow this order, do not skip a step:
  1. Call RagIngestDirectory(knowledge_base="default", path="C:/tmp/digitorn-tutorials/rag-kb", extensions=[".md"]).
  2. Call RagQuery(knowledge_base="default", query="hooks Digitorn", top_k=3).
  3. Based on the RagQuery results, answer: How do hooks work in Digitorn? Cite the source file path for each fact.'
```

The hyper-explicit user prompt is needed because
`gpt-5-mini` often skips a tool when it thinks the data
is available from a previous call. A larger model
(`gpt-4o`, Claude Sonnet) would follow the system_prompt's
bootstrap order without hand-holding.

## Real session transcript (session `test-fee8063b`)

**Tool call 1: RagIngestDirectory** (success)

```json
params:
{
  "knowledge_base": "default",
  "path": "C:/tmp/digitorn-tutorials/rag-kb",
  "extensions": [".md"],
  "recursive": true,
  "max_files": 1000
}

result:
{
  "knowledge_base": "default",
  "directory": "C:\\tmp\\digitorn-tutorials\\rag-kb",
  "documents": 3,
  "chunks": 10,
  "added": 10
}
```

3 documents ingested, chunked into 10 passages, all
written to the in-memory Qdrant collection.

**Tool call 2: RagQuery** (success)

```json
params:
{
  "knowledge_base": "default",
  "query": "hooks Digitorn",
  "top_k": 3
}

result:
{
  "knowledge_base": "default",
  "query": "hooks Digitorn",
  "strategy": "hybrid",
  "results": [
    {
      "text": "# Hooks in Digitorn\n\nThe Hooks V2 engine intercepts every tool call before and after execution. Each hook declares an event (tool_start, tool_end, turn_start, turn_end, session_start, session_end, pre_compact, error, approval_request, agent_spawn, agent_complete), a condition, and an action.",
      "score": 0.0328,
      "doc_id": "C:\\tmp\\digitorn-tutorials\\rag-kb\\hooks.md:section:0:chunk:0",
      ...
    },
    ...
  ],
  "count": 3,
  "cache_hit": false
}
```

Strategy is `hybrid` (semantic + BM25 reranking, the
pipeline default). Cache miss on this first query;
subsequent identical queries hit the semantic cache.

**Assistant's final reply** (verbatim):

```
Plan: I ingested the Markdown docs, ran a search for
"hooks Digitorn", and will answer based on the retrieved
results (citing source file paths).

What I did:
- Ingested C:/tmp/digitorn-tutorials/rag-kb (Markdown).
  Ingest result: 3 documents, 10 chunks added to KB
  "default".
- Queried the KB for "hooks Digitorn" (top_k=3). The
  query returned relevant passages from:
    - C:\tmp\digitorn-tutorials\rag-kb\hooks.md
    - C:\tmp\digitorn-tutorials\rag-kb\modules.md (less
      relevant)

Answer - How hooks work in Digitorn (facts with source
file path):
- The Hooks V2 engine intercepts every tool call both
  before and after execution.
  [C:\tmp\digitorn-tutorials\rag-kb\hooks.md]
- Each hook is defined by three parts: an event it
  listens for, a condition that must hold, and an action
  to run when triggered.
  [C:\tmp\digitorn-tutorials\rag-kb\hooks.md]
- The supported hook events include: tool_start,
  tool_end, turn_start, turn_end, session_start,
  session_end, pre_compact, error, approval_request,
  agent_spawn, and agent_complete.
  [C:\tmp\digitorn-tutorials\rag-kb\hooks.md]
- Hooks fire during the agent loop (i.e., they run as
  the agent executes its loop of actions).
  [C:\tmp\digitorn-tutorials\rag-kb\hooks.md]
- Hook state is isolated per session, so concurrent
  users/sessions do not see or interfere with each
  other's hooks.
  [C:\tmp\digitorn-tutorials\rag-kb\hooks.md]
```

Every fact is grounded in the indexed document, with the
source file path inline. Zero hallucination.

## What we proved

| Claim | Status |
|---|---|
| `RagCreateKnowledgeBase` materialises a fresh KB | verified, `created: true` first call, `already_exists: true` on retry |
| `RagIngestDirectory` chunks + embeds files in a directory | verified, 3 docs → 10 chunks |
| `RagQuery` returns hybrid search results with `doc_id` pointing at the source file | verified |
| The agent grounds its answer in retrieved chunks and cites paths | verified, 5/5 facts cited inline |
| `auto_index.on_start` does NOT auto-populate a per-app KB | verified by negative test (KB stayed empty until agent ingested manually) |
| Shared rag module persists KBs across deploys | verified, second `RagCreateKnowledgeBase` returned `already_exists: true` |

## Performance notes

- First embedding pass is slow: FastEmbed downloads the
  ONNX model weights (~50 MB for `minilm-l12`) on first
  use. Subsequent ingests reuse the cached model and run
  in ~1-2s for tens of small files.
- The default `embedding_model: minilm-l12` is a 384-dim
  multilingual sentence transformer. For higher quality
  use `bge-m3` (1024-dim, slower) or `nomic-v1.5` (768-dim,
  matryoshka).
- Hybrid search runs BM25 lexical scoring alongside the
  vector search, then reranks. Disable with
  `pipeline.strategy: "semantic"` for pure vector, or
  `"bm25"` for keyword-only.

## When to reach for this

- Documentation Q&A: the agent answers from your project
  docs, code comments, or wiki, with citations the user
  can verify.
- Compliance / audit: ground every answer in a primary
  source so the auditor can re-read the cited passage.
- Long-running projects: an agent that re-indexes
  changing files (set `sources[0].watch: true`) so its
  knowledge stays fresh without manual re-ingest.

For sessionless, one-shot semantic search (no agent
loop), call `rag.query` directly via the HTTP API
[`POST /api/modules/rag/execute`](../reference/runtime/module-api.md).
For structured data search (SQL tables), the
`rag.sql_query` action runs text-to-SQL grounded by the
table schema embedded into the KB.

## Production deployment notes

- Use a persistent backend in production:
  `backend: {type: qdrant, path: "<workspace>/qdrant_data"}`
  for local, or `{type: qdrant, url: "https://...", api_key: "..."}`
  for Qdrant Cloud.
- Pre-build the KB offline with
  `knowledge_base/build.py` so the agent does not pay
  the ingest cost on first user turn.
- Set `cache.enabled: true` (default) to dedupe repeated
  identical queries within a session.
- For multilingual content beyond English/French, swap
  the embedding model to `bge-m3` (100+ languages with
  comparable quality).
