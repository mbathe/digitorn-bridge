---
id: module-concept-index
title: "index module - overview"
type: module-concept
module: index
isolation: shared
keywords: [index, index-module, register_source, register_extractor, scan, query, relations, context, invalidate]
version: 1.0.0
---

# `index` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 7 visible, 0 internal

## Description (from class docstring)

Index module - unified knowledge index for all Digitorn modules.

The index is a **brain without hands**: it stores, searches, and links
knowledge about all data sources, but it never reads content directly.
Instead, it delegates to the owning module (filesystem, database, storage)
via the service bus.

Actions:
  - register_source: Register a new data source to index.
  - register_extractor: Register a custom extractor from another module.
  - scan: Scan a source and update the index (incremental by default).
  - query: Full-text search across all indexed entries.
  - relations: Explore the relation graph from an entry.
  - context: Get optimal LLM context for a target (the killer feature).
  - invalidate: Remove stale entries.

Daemon integration:
  - Subscribes to ``digitorn.module.*.action_completed`` events.
  - Auto-invalidates entries when filesystem writes/edits/deletes occur.
  - Persists index to disk via ``state_snapshot`` / ``restore_state``.

## Configuration

Set under `modules.index.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `register_source` | `IndexRegisterSource` |  | low | Register a data source to be indexed. Sources are owned by a specific module (filesystem, database, etc.). After regi... |
| `register_extractor` | `IndexRegisterExtractor` |  | low | Register a custom extractor provided by another module. The extractor will be called via the service bus during scan. |
| `scan` | `IndexScan` |  | low | Scan a registered source and update the index. Incremental by default - only processes changed content. Use force=tru... |
| `query` | `IndexQuery` |  | low | Search the index for entries matching a query. Searches across names, signatures, and summaries. Returns entries sort... |
| `relations` | `IndexRelations` |  | low | Explore the relation graph from an entry. Shows what an entry imports/calls/references and what references it. |
| `context` | `IndexContext` |  | low | Get optimal context for an LLM to work on a target. Returns the target's signature, location, and related entries (de... |
| `invalidate` | `IndexInvalidate` |  | low | Invalidate (remove) entries from the index. Use source_id to clear an entire source, or path for a specific file. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: index
      actions: [register_source, register_extractor, scan, query, relations, context, invalidate]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {index: [register_source, register_extractor, scan, query, relations]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/index-*.md`.
