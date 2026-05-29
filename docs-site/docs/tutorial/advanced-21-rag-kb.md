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
with citations** loop.

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
            path="./rag-kb",
            extensions=[".md"].
         Wait for it to finish (it returns the chunk count).
         RagIngestDirectory FAILS with "Knowledge base
         'default' not found" if you skip step (a).

      Then, on EVERY user question:
      1. Call RagQuery with knowledge_base="default",
         query=<the user question rephrased as a search>,
         top_k=3.
      2. Answer based on what RagQuery returned. Cite the
         file path for each fact. If the result list is
         empty, say so explicitly instead of guessing.

tools:
  modules:
    rag:
      config:
        embedding_model: minilm-l12
        backend:
          type: qdrant
          # Empty path = in-memory (no disk persistence).
          # Production sets a path under the workspace for
          # durability.
          path: ""
        sources:
          - type: file
            path: "./rag-kb"
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
- **`auto_index.on_start: true` does NOT run for shared
  modules.** The rag module is a shared singleton: its
  start phase runs once at daemon boot, before any app
  exists. Per-app `sources:` only land via per-app config
  updates, which do not re-run the ingest. So `sources:`
  and `auto_index:` are forward-looking; the agent has to
  ingest itself (next section), or you populate the KB
  offline via the tool.
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
`./rag-kb/`:

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
  1. Call RagIngestDirectory(knowledge_base="default", path="./rag-kb", extensions=[".md"]).
  2. Call RagQuery(knowledge_base="default", query="hooks Digitorn", top_k=3).
  3. Based on the RagQuery results, answer: How do hooks work in Digitorn? Cite the file path for each fact.'
```

The hyper-explicit user prompt is needed because smaller
models often skip a tool when they think the data is
available from a previous call. A larger model would follow
the system_prompt's bootstrap order without hand-holding.

## Sample flow

**Tool call 1: `RagIngestDirectory`**

```json
params:
{
  "knowledge_base": "default",
  "path": "./rag-kb",
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

**Tool call 2: `RagQuery`**

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

**Assistant's final reply**:

```text
Plan: I ingested the Markdown docs, ran a search for
"hooks Digitorn", and will answer based on the retrieved
results (citing file paths).

What I did:
- Ingested ./rag-kb (Markdown).
  Ingest result: 3 documents, 10 chunks added to KB
  "default".
- Queried the KB for "hooks Digitorn" (top_k=3). The
  query returned relevant passages from:
    - ./rag-kb\hooks.md
    - ./rag-kb\modules.md (less
      relevant)

Answer - How hooks work in Digitorn (facts with file path):
- The Hooks V2 engine intercepts every tool call both
  before and after execution.
  [./rag-kb\hooks.md]
- Each hook is defined by three parts: an event it
  listens for, a condition that must hold, and an action
  to run when triggered.
  [./rag-kb\hooks.md]
- The supported hook events include: tool_start,
  tool_end, turn_start, turn_end, session_start,
  session_end, pre_compact, error, approval_request,
  agent_spawn, and agent_complete.
  [./rag-kb\hooks.md]
- Hooks fire during the agent loop.
  [./rag-kb\hooks.md]
- Hook state is isolated per session.
  [./rag-kb\hooks.md]
```

Every fact is grounded in the indexed document, with the
file path inline.

## When to reach for this

- Documentation Q&A: the agent answers from your project
  docs, code comments, or wiki, with citations the user
  can verify.
- Compliance / audit: ground every answer in a primary
  reference so the auditor can re-read the cited passage.
- Long-running projects: an agent that re-indexes
  changing files (set `sources[0].watch: true`) so its
  knowledge stays fresh without manual re-ingest.

For sessionless, one-shot semantic search (no agent
loop), call `rag.query` directly via the HTTP API
[`POST /api/modules/rag/execute`](../reference/api/rest.md).
For structured data search (SQL tables), the
`rag.sql_query` action runs text-to-SQL grounded by the
table schema embedded into the KB.

## Production deployment

- Use a persistent backend in production:
  `backend: {type: qdrant, path: "<workspace>/qdrant_data"}`
  for local, or `{type: qdrant, url: "https://...", api_key: "..."}`
  for Qdrant Cloud.
- Pre-build the KB offline with the
  tool so the agent does not pay the ingest cost on first
  user turn.
- Set `cache.enabled: true` (default) to dedupe repeated
  identical queries within a session.
- For multilingual content beyond English/French, swap
  the embedding model to `bge-m3` (100+ languages).
