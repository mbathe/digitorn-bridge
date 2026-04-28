---
id: module-concept-filesystem
title: "filesystem module - overview"
type: module-concept
module: filesystem
isolation: shared
keywords: [filesystem, filesystem-module, read, write, edit, glob, grep]
version: 1.0.0
---

# `filesystem` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 5 visible, 0 internal

## Description (from class docstring)

Filesystem module - 5 ultra-powerful actions for AI agents.

Design:
  1. MINIMAL params → LLM makes fewer mistakes
  2. POWERFUL implementations → fuzzy matching, auto-detection, smart errors
  3. RICH feedback → metadata, diffs, recovery hints for preview/frontend
  4. ERROR-FRIENDLY → closest matches, suggestions on failure

Actions:
  - Read: text, images, PDFs, notebooks with auto-detection
  - Write: creates parent dirs automatically, atomic writes
  - Edit: fuzzy matching + insert_at_line + recovery hints
  - Grep: ripgrep-powered search with multiline + context
  - Glob: pattern-based file finding with type filtering

Removed (use Bash instead):
  - ls, mv, cp, rm, mkdir, insert, find, file_stat, undo

> Class-level summary: Filesystem module with 5 ultra-powerful actions.

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `read` | `Read` |  | low | Read a file from the local filesystem. |
| `write` | `Write` |  | low | Write a file to the local filesystem. |
| `edit` | `Edit` |  | low | Find-and-replace in a file. |
| `glob` | `Glob` |  | low | Find files by name pattern. |
| `grep` | `Grep` |  | low | Search file contents for a regex pattern. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: filesystem
      actions: [read, write, edit, glob, grep]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {filesystem: [read, write, edit, glob, grep]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/filesystem-*.md`.
