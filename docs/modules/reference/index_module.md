---
id: index-module
title: index Module
sidebar_label: index
sidebar_position: 6
description: System module — unified knowledge index across data sources (filesystem, database, custom).
---

# index

System module that maintains a unified, searchable knowledge
index across every data source in an app — files, database
rows, custom payloads. The index is a **brain without hands**:
it stores, searches, and links knowledge but never reads
content directly. Reads go through the owning module via the
ServiceBus.

| Property | Value | Source |
|----------|-------|--------|
| Module id | `index` | `module.py:66` |
| Version | `1.0.0` | `module.py:67` |
| Type | `system` (auto-injected, hidden from agents) | `module.py:69` |
| Config model | `IndexConfig` (`extra: forbid`) | `module.py:45` |
| Supported platforms | `Platform.ALL` | `module.py:68` |

## Role in the architecture

Three responsibilities:

1. **Source registry** — every other module (filesystem,
   database, ...) registers its data sources with `register_source`.
2. **Extraction + embedding** — on `scan`, the index calls the
   source's extractor (built-in or registered via
   `register_extractor`), embeds the resulting entries with
   FastEmbed (`paraphrase-multilingual-MiniLM-L12-v2`,
   384 dims), and stores them in an in-memory Qdrant index.
3. **Knowledge retrieval** — `query` (semantic search),
   `relations` (import / call / reference graph), `context`
   (LLM-ready context bundle for a target file or symbol).

Used internally by the context builder for tool discovery and
codebase-aware context. **Does not replace `filesystem`** —
`Glob`, `Grep`, `Read` always hit the real filesystem; the
index is purely a search overlay.

## Auto-injection

`bootstrap.py`. When `tools.modules.filesystem` is loaded AND
`runtime.workdir` is set, the index module is auto-injected
and a workspace source is auto-registered + scanned.

## The 7 actions

`module.py`. All `permissions=["index:admin"]` (so they're
hidden from regular agents — only the runtime calls them).

| Tool | Source | Purpose |
|------|--------|---------|
| `index.register_source` | `:273` | Register a new data source (id, owning module, root, extractor, optional watch). |
| `index.register_extractor` | `:316` | Register a custom extractor backed by another module's action (called via the ServiceBus during `scan`). |
| `index.scan` | `:343` | Scan a registered source and update the index. Incremental by default — only processes changed content. |
| `index.query` | `:546` | Semantic search across names, signatures, summaries. |
| `index.relations` | `:577` | Explore the relation graph from an entry — imports, calls, references. |
| `index.context` | `:613` | Get LLM-optimal context for a target file or symbol — its signature, location, and related entries. The "killer feature". |
| `index.invalidate` | `:760` | Remove entries (whole source via `source_id`, or single file via `path`). |

## Daemon integration

`module.py:83` `on_event`. The index subscribes to
`digitorn.module.*.action_completed` events from the
ServiceBus and auto-invalidates entries on filesystem
mutations:

| Event | Action |
|-------|--------|
| `filesystem.write` / `filesystem.edit` / `filesystem.create` | Re-extract + re-embed the touched file. |
| `filesystem.delete` | `invalidate(path=...)`. |
| `filesystem.rename` | invalidate old path, scan new path. |

State snapshot + restore (`module.py:244`) persists the index
to disk so it survives daemon restarts.

## Constraints

`module.py:789`. Two scopes:

| Constraint | Type | Default | Description |
|------------|------|---------|-------------|
| `allowed_sources` | `string_list` | unrestricted | Source ids this app can register / scan / query. |
| `max_entries` | `integer` | `50000` | Maximum entries per source. |

```yaml
tools:
  modules:
    index:
      constraints:
        allowed_sources: [workspace, docs]
        max_entries: 100000
```

## Configuration

The index module is auto-injected and accepts no required
config. The `workspace` field on `IndexConfig` is daemon-set
from `runtime.workdir`.

```yaml
runtime:
  workdir: /path/to/project    # ← what the index scans

tools:
  modules:
    filesystem: {}              # triggers auto-injection of index
```

## Cross-references

- App-config block reference (`tools.modules.index`):
  [App Configuration → tools.modules](../../app-language/02-app-config.md#toolsmodules--module-config)
- The agent's RAG-shaped knowledge module (separate from
  `index`): [RAG Module](../../app-language/37-rag.md)
- ServiceBus event protocol (what `index.on_event` listens
  to): [Hooks](../../app-language/31-tool-hooks.md)
