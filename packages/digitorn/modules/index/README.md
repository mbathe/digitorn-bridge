# Index Module

Unified knowledge index — stores, searches, and links entries from all data
sources (filesystem, database, storage).

## Overview

The Index module is the **brain** of the Digitorn platform. It doesn't read
content directly — instead, it delegates to owning modules (filesystem, database,
etc.) via the service bus, then extracts structured entries (functions, classes,
tables, files) and builds a searchable relation graph.

Key capabilities:

- **`register_source`** — register any data source (project, database, bucket).
- **`scan`** — extract entries and relations (incremental by default).
- **`query`** — full-text search across all indexed entries.
- **`relations`** — explore the dependency/usage graph from any entry.
- **`context`** — get optimal LLM context for a target, trimmed to token budget.
- **`invalidate`** — remove stale entries (also happens automatically on events).

## Actions

| Action | Description | Risk | Permissions |
|--------|-------------|------|-------------|
| `register_source` | Register a data source | Low | `index:admin` |
| `register_extractor` | Register a custom extractor | Low | `index:admin` |
| `scan` | Scan a source and update the index | Low | `index:write` |
| `query` | Full-text search across entries | Low | `index:read` |
| `relations` | Explore the relation graph | Low | `index:read` |
| `context` | Get optimal LLM context for a target | Low | `index:read` |
| `invalidate` | Remove stale entries | Low | `index:write` |

## Architecture

```
                 ┌──────────────┐
                 │  Index Store │  (in-memory, dict-based)
                 │              │
                 │ entries{}    │  O(1) lookup by id/path/name/kind
                 │ fts{}        │  Inverted index for full-text search
                 │ relations{}  │  Adjacency-list relation graph
                 └──────┬───────┘
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
    ┌───────────┐ ┌───────────┐ ┌───────────┐
    │ Python    │ │ Text      │ │ Remote    │
    │ Extractor │ │ Extractor │ │ Extractor │
    │ (AST)     │ │ (fallback)│ │ (via bus) │
    └───────────┘ └───────────┘ └───────────┘
```

### Extractors

Extractors transform raw content into `IndexEntry` + `Relation` lists.

| Extractor | What it produces |
|-----------|-----------------|
| `python` | Functions, classes, imports with signatures + docstrings |
| `text` | Single file entry with line count and hash |
| Custom | Anything — registered by other modules at runtime |

### Automatic Change Detection (Watcher)

The index integrates with the **SourceWatcherService** to automatically detect
and re-index changes. When `register_source` is called with `watch=true`, the
watcher monitors the source in real-time:

```
    External change          Watcher backend           Event bus            Index module
    ───────────────          ───────────────           ─────────            ────────────
    IDE save                 FilesystemWatcher  ──→  digitorn.watcher.     on_event()
    git pull                 (inotify/fsevents)       {source_id}.          → invalidate
    DB row insert            PollingWatcher     ──→   file_created          → re-index
    API data change          ServiceBusPoller   ──→   file_modified
                                                      file_deleted
```

Two watch modes control lifecycle:

| Mode | Behavior |
|------|----------|
| `ephemeral` | Watch stops when the application disconnects |
| `persistent` | Watch survives daemon restarts (saved in state snapshot) |

### Event-Driven Invalidation

The index reacts to two event sources:

1. **Watcher events** (`digitorn.watcher.*.file_*`) — external changes detected
   by the OS or polling backends.
2. **Module action_completed** (`digitorn.module.*.action_completed`) — changes
   made through the daemon API (filesystem.write, database.insert, etc.).

Both paths funnel into the same invalidate + re-index logic. No manual cleanup
needed.

### State Persistence

The full index (sources, entries, relations, persistent watch configs) is
persisted via `JsonStateStore` to `~/.digitorn/state/index.state.json`.
The `ModuleLifecycleManager` calls `state_snapshot()` on stop and
`restore_state()` on start. Zero data loss across restarts. Persistent
watches are automatically restarted via the injected `SourceWatcherService`.

## LLM Integration

The `context` action is the primary tool for LLM agents:

1. Call `index.context` with a target (function name, file path, or search query)
2. Get back the target's signature, content, dependencies, and callers
3. All trimmed to fit your token budget
4. Use the returned context to make precise edits via `filesystem.edit`

This eliminates the need for agents to manually read and navigate files.

### Typical Workflow

```
1. index.register_source(watch=true)  →  register project with auto-watching
2. index.scan                          →  build the initial index
3. index.context                       →  get smart context for a target
4. filesystem.edit                     →  make precise edits
   ↓                                      (watcher auto-re-indexes)
5. index.context                       →  context is already up-to-date
```
