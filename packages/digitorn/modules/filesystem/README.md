# Filesystem Module

Five filesystem operations: **read**, **write**, **edit**, **grep**, **glob**.

## Overview

The Filesystem module gives AI agents full control with **minimal noise** (5 tools vs 15).
Every action designed for agent-friendly operation:

- **`read`** returns content with line numbers; auto-detects images, PDFs, notebooks
- **`write`** creates/overwrites files; auto mkdir -p, atomic writes
- **`edit`** surgical text replacement; fuzzy matching, insert_at_line, closest matches on error
- **`grep`** ripgrep-powered regex search with multiline, context, smart error hints
- **`glob`** pattern-based file finding with type filtering, suggestions if no matches

**For operations not in this list (ls, mv, cp, rm, mkdir, etc.), use Bash.**

## Actions

| Action | Description | Risk | Permissions |
|--------|-------------|------|-------------|
| `read` | Read file with line numbers; auto-detects images/PDFs/notebooks | Low | `fs.read` |
| `write` | Create/overwrite file; auto mkdir -p, atomic writes | Medium | `fs.write` |
| `edit` | Fuzzy replace old_string→new_string; insert_at_line support | Medium | `fs.read`, `fs.write` |
| `grep` | Regex search via ripgrep; multiline, context, smart errors | Low | `fs.read` |
| `glob` | Find files by pattern; type filtering, suggestions | Low | `fs.read` |

## Removed Actions (Use Bash)

The following were removed in v2.1.0 to reduce LLM noise (15→5 tools):
- `ls` → `bash ls`
- `mv` → `bash mv`
- `cp` → `bash cp`
- `rm` → `bash rm`
- `mkdir` → `bash mkdir -p` (or use Write auto-mkdir)
- `insert` → Edit with `insert_at_line`
- `find` → Glob with type filtering
- `file_stat` → `bash stat` or Read metadata
- `undo` → git via bash

## Constraints

The module supports constraints that applications set in their YAML definition
to restrict what the agent can do:

| Constraint | Type | Scope | Description |
|------------|------|-------|-------------|
| `paths` | `string_list` | Universal | Allowed path prefixes - all operations are restricted to these directories. |
| `max_file_size` | `size` | Module | Maximum file size for read/write (e.g. `"50MB"`). Default: `"100MB"`. |

Example in an app definition:

```yaml
- module: filesystem
  actions: [read, write, edit, grep, glob]
  constraints:
    paths: ["{{workspace}}"]
    max_file_size: "50MB"
```

## Performance

- **Sync I/O** for files under 512 KB (no thread-pool overhead).
- **Subprocess** for `grep` - tries `rg` (ripgrep), then `grep`, then Python fallback.
- **No unnecessary encoding** - reads/writes bytes directly when possible.

## Quick Start

```yaml
actions:
  - id: read-config
    module: filesystem
    action: read
    params:
      path: "{{workspace}}/config.yaml"
```

## Requirements

No external dependencies. Uses only Python standard library (`pathlib`, `shutil`,
`os`, `stat`, `re`). Optional: `ripgrep` for faster `grep` action.

## Platform Support

| Platform | Status |
|----------|--------|
| Linux | Supported |
| macOS | Supported |
| Windows | Supported |
