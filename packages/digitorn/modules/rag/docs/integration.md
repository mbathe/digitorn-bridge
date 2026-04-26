# rag — Integration Guide

`rag` gives agents a **knowledge-base primitive**: ingest files into
named collections, run semantic queries, return ranked snippets. Backed
by a Qdrant on-disk store (default) or an in-memory fallback.

## Actions (14 total — see `docs/actions.md`)

Grouped by lifecycle:

| Group | Actions |
|---|---|
| KB management | `create_knowledge_base`, `delete_knowledge_base`, `list_knowledge_bases`, `get_knowledge_base` |
| Ingestion | `add_documents`, `ingest_path`, `ingest_url` |
| Query | `query`, `search_with_metadata`, `similar_to` |
| Curation | `update_document`, `delete_document`, `list_documents`, `stats` |

## Critical config detail — the `config:` wrapper

```yaml
modules:
  rag:
    config:                        # ← required wrapper
      backend:
        type: qdrant
        path: "./.digitorn/kb/.qdrant"
```

**Without the wrapper**, the keys are silently dropped (the
`ModuleBlock` schema only reads `config`, `setup`, `constraints`,
`middleware`). This was a real production bug — see `CLAUDE.md`
"Module config YAML structure" for the full story.

## Shared-instance semantics

`rag` is `isolation: "shared"` — one module instance per daemon. Every
app that declares `rag` gets the **same** Qdrant store back. On
`on_config_update` the module compares the incoming backend path
against its current one and rebuilds the backend only if it changed.
It then re-discovers existing collections on disk so the app sees
knowledge bases that earlier apps (or offline tools like
`knowledge_base/build.py`) already populated.

This is intentional: multiple agent apps share a common KB surface.

## Constraints

| Constraint | Type | Scope | Default | Purpose |
|---|---|---|---|---|
| `max_knowledge_bases` | `integer` | module | 10 | Cap the number of distinct KBs this app can create. |
| `max_documents` | `integer` | module | 10000 | Cap documents per KB. |
| `paths` | `string_list` | universal | `{{workspace}}` | Restrict where `ingest_path` can read from. |
| `allowed_collections` | `string_list` | module | — | Whitelist of KB names this app may touch. |

## Typical flow

```
offline — knowledge_base/build.py
        │
        ▼
Qdrant collection on disk (./.digitorn/kb/.qdrant)
        │
        ▼
daemon boots → rag module discovers collections
        │
        ▼
agent → rag.query(kb="digitorn-concepts", query="How do hooks work?")
        │
        ▼
top-k snippets + metadata returned to the agent
```

## When NOT to use

- Pure vector search without the "knowledge base" abstraction → use
  `vector` directly.
- Very small static FAQs (a dozen entries) — inlining them in the
  system prompt is simpler.

## Related

- `modules/vector` — the layer `rag` builds on
- `knowledge_base/build.py` — offline ingestion tool
- `modules/rag/module.py::on_config_update` — backend swap logic
