---
id: index-module
title: Index Module
sidebar_label: index
sidebar_position: 6
description: System module -- workspace indexing for semantic code search via FastEmbed + Qdrant.
---

# index

System module that indexes workspace files for semantic search. Automatically injected when the filesystem module is present and a workspace is configured. Used internally by the context builder to enhance tool discovery with codebase-aware context.

| Property | Value |
|----------|-------|
| **Module ID** | `index` |
| **Version** | `1.0.0` |
| **Type** | system (auto-injected, hidden from agents) |
| **Dependencies** | `fastembed`, `qdrant-client` |

---

## Role in the Architecture

The index module provides:

1. **Workspace scanning** -- incrementally scans source files, extracts content, and builds a searchable index.
2. **Semantic embeddings** -- uses FastEmbed (`paraphrase-multilingual-MiniLM-L12-v2`, 384 dimensions) to embed file content, function signatures, class names, and docstrings.
3. **Relation tracking** -- records import/export relationships between files for dependency-aware context retrieval.
4. **Context provision** -- when the agent needs context about a file or symbol, the index provides the most relevant entries.

---

## How It Works

At bootstrap, if the filesystem module is present:

1. The compiler detects `execution.workspace` (or defaults to the YAML file's directory).
2. The index module is auto-injected.
3. `_auto_index_workspace()` registers the workspace as a source and runs an incremental scan.
4. The scanned entries are embedded and stored in the in-memory Qdrant index.

The index is used by `search_tools` in the context builder to find relevant code context. It does NOT replace the filesystem module -- `Glob` and `Grep` always hit the real filesystem, not the index.

---

## Actions (7)

These actions are for internal use and are hidden from agents:

| Action | Description |
|--------|-------------|
| `register_source` | Register a data source for indexing |
| `register_extractor` | Register a custom content extractor |
| `scan` | Scan a source and update the index (incremental by default) |
| `query` | Semantic search across indexed entries |
| `relations` | Explore the import/export graph from an entry |
| `context` | Get optimal LLM context for a target file or symbol |
| `invalidate` | Remove entries from the index |

---

## Constraints

| Constraint | Type | Description |
|------------|------|-------------|
| `allowed_sources` | string_list | Source IDs this application can access. |
| `max_entries` | integer | Maximum number of entries per source. Default: `50000`. |

### Example App YAML

```yaml
modules:
  - module: index
    constraints:
      allowed_sources: [workspace, docs]
      max_entries: 100000
```
---

## Configuration

The index module is auto-injected and does not require explicit configuration. It reads its workspace from `execution.workspace` set in the app YAML. Scanning and embedding happen automatically at bootstrap.

```yaml
execution:
  workspace: /path/to/project
```