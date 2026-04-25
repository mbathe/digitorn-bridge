# Index Module — Action Reference

Complete reference for all 7 actions exposed by the index module.

---

## register_source

Register a new data source to be indexed.

**Permissions:** `index:admin`
**Risk level:** Low

### Parameters

| Name           | Type    | Required | Default  | Description                                                          |
|----------------|---------|----------|----------|----------------------------------------------------------------------|
| `source_id`    | string  | yes      | —        | Unique identifier (e.g. `"backend_project"`).                        |
| `module_id`    | string  | yes      | —        | Module that owns the source (`"filesystem"`, `"database"`, etc.).    |
| `root`         | string  | yes      | —        | Root path or URI of the source.                                      |
| `extractor`    | string  | no       | `"auto"` | Extractor to use: `"auto"`, `"text"`, `"python"`, or custom.        |
| `scan_pattern` | string  | no       | `"**/*"` | Glob/filter pattern for scanning.                                    |
| `metadata`     | object  | no       | `{}`     | Extra config passed to the extractor.                                |
| `watch`        | boolean | no       | `false`  | Enable automatic change detection on this source.                    |
| `watch_mode`   | string  | no       | `"ephemeral"` | `"ephemeral"` (dies with app) or `"persistent"` (survives restarts). |

### Watch Behavior

When `watch=true`:

- **Filesystem sources** (`module_id="filesystem"`): Uses the `FilesystemWatcher`
  (inotify/FSEvents via `watchfiles`) for near-instant detection, or `PollingWatcher`
  as fallback.
- **Non-filesystem sources** (`module_id="database"`, etc.): Uses the
  `ServiceBusPollingWatcher` which delegates `list_items` and `checksum` calls
  to the owning module. Requires service bus context.
- The watcher publishes events to the event bus on topics
  `digitorn.watcher.{source_id}.file_created|modified|deleted`.
- The index module subscribes to these events and automatically invalidates
  + re-indexes affected entries.

### Returns

```json
{
  "source_id": "backend_project",
  "module_id": "filesystem",
  "root": "/home/user/project",
  "extractor": "auto",
  "watch": true,
  "watch_mode": "persistent",
  "watch_status": "active",
  "message": "Source 'backend_project' registered. Call scan to index it."
}
```

`watch_status` values:

| Status                | Meaning                                                      |
|-----------------------|--------------------------------------------------------------|
| `"active"`            | Watcher started successfully.                                |
| `"disabled"`          | `watch=false`, no watcher started.                           |
| `"no_watcher_service"`| Daemon watcher service not available.                        |
| `"error"`             | Watcher failed to start (e.g. missing service bus for DB).   |

---

## register_extractor

Register a custom extractor provided by another module.

**Permissions:** `index:admin`
**Risk level:** Low

### Parameters

| Name             | Type   | Required | Default | Description                                         |
|------------------|--------|----------|---------|-----------------------------------------------------|
| `name`           | string | yes      | —       | Unique name (e.g. `"sql"`, `"pdf"`).                |
| `module_id`      | string | yes      | —       | Module providing extraction logic.                  |
| `extract_action` | string | yes      | —       | Action name on the module that performs extraction.  |

---

## scan

Scan a registered source and update the index. Incremental by default.

**Permissions:** `index:write`
**Risk level:** Low

### Parameters

| Name        | Type    | Required | Default | Description                                  |
|-------------|---------|----------|---------|----------------------------------------------|
| `source_id` | string  | yes      | —       | Source to scan (must be registered).          |
| `force`     | boolean | no       | `false` | Force full rescan even if hashes match.      |

### Returns

```json
{
  "source_id": "backend_project",
  "files_scanned": 42,
  "added": 150,
  "updated": 3,
  "unchanged": 120,
  "errors": 0,
  "total_entries": 273
}
```

---

## query

Full-text search across all indexed entries.

**Permissions:** `index:read`
**Risk level:** Low

### Parameters

| Name        | Type    | Required | Default | Description                                                 |
|-------------|---------|----------|---------|-------------------------------------------------------------|
| `q`         | string  | yes      | —       | Search query (matches names, signatures, summaries).        |
| `kind`      | string  | no       | —       | Filter by entry kind: `"file"`, `"function"`, `"class"`.    |
| `source_id` | string  | no       | —       | Filter to a specific source.                                |
| `limit`     | integer | no       | `20`    | Max results (1-100).                                        |

### Returns

```json
{
  "results": [
    {
      "entry_id": "abc123",
      "name": "calculate_discount",
      "kind": "function",
      "path": "/project/pricing.py",
      "signature": "def calculate_discount(price: float, percent: float) -> float",
      "summary": "Apply a percentage discount to a price.",
      "score": 1.5
    }
  ],
  "count": 1,
  "query": "calculate_discount"
}
```

---

## relations

Explore the relation graph from an entry.

**Permissions:** `index:read`
**Risk level:** Low

### Parameters

| Name        | Type    | Required | Default  | Description                                                                    |
|-------------|---------|----------|----------|--------------------------------------------------------------------------------|
| `entry_id`  | string  | yes      | —        | Entry ID (from a previous query result).                                       |
| `direction` | string  | no       | `"both"` | `"in"` (who references me), `"out"` (what I reference), `"both"`.             |
| `kind`      | string  | no       | —        | Filter by relation kind: `"imports"`, `"calls"`, `"contains"`, `"inherits"`.  |
| `depth`     | integer | no       | `1`      | Traversal depth (1-5).                                                         |

---

## context

Get optimal LLM context for a target, trimmed to token budget. **This is the
primary action for LLM agents.**

**Permissions:** `index:read`
**Risk level:** Low

### Parameters

| Name                | Type    | Required | Default | Description                                    |
|---------------------|---------|----------|---------|------------------------------------------------|
| `target`            | string  | yes      | —       | Entry ID, file path, or search query.          |
| `token_budget`      | integer | no       | `4000`  | Max tokens for returned context (100-100000).  |
| `include_relations` | boolean | no       | `true`  | Include dependencies and callers.              |
| `depth`             | integer | no       | `1`     | Relation traversal depth (1-3).                |

### Returns

```json
{
  "target": { "entry_id": "...", "name": "calculate_discount", "...": "..." },
  "content": "def calculate_discount(price, percent):\n    ...",
  "dependencies": [
    { "name": "validate_price", "relation": "calls", "...": "..." }
  ],
  "callers": [
    { "name": "process_order", "relation": "calls", "...": "..." }
  ],
  "tokens_used": 1200,
  "token_budget": 4000,
  "entries_included": 3
}
```

---

## invalidate

Remove stale entries from the index.

**Permissions:** `index:write`
**Risk level:** Low

### Parameters

| Name        | Type   | Required | Default | Description                              |
|-------------|--------|----------|---------|------------------------------------------|
| `source_id` | string | no       | —       | Clear all entries from this source.      |
| `path`      | string | no       | —       | Clear all entries for this file path.    |

At least one of `source_id` or `path` must be provided.
