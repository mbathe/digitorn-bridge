---
id: filesystem
title: Filesystem Module
sidebar_label: filesystem
sidebar_position: 1
description: 5 ultra-powerful filesystem actions for agents - Read, Write, Edit, Glob, Grep.
---

# filesystem

Agent-optimized filesystem operations. **5 actions**, short PascalCase tool names (same as Claude Code), minimal visible params + hidden implementation details for reliability.

| Property | Value |
|----------|-------|
| **Module ID** | `filesystem` |
| **Version** | `2.0.0` |
| **Type** | system |
| **Platforms** | Linux, macOS, Windows |
| **Dependencies** | None (stdlib). Optional: `ripgrep` for faster grep. |
| **Permissions** | `fs.read`, `fs.write`, `fs.delete`, `fs.list` |

---

## Design Philosophy

Inspired by Claude Code's tool surface:

1. **Minimal visible params** → LLM makes fewer mistakes. All non-essential params are hidden from the JSON schema.
2. **Powerful implementation** → hidden params + smart defaults handle encoding, fuzzy matching, recovery.
3. **Safety-first** → `Edit` refuses to overwrite files that weren't `Read` first (>500 bytes).
4. **No legacy actions** - use Bash for `ls`, `mv`, `cp`, `rm`, `mkdir`, `stat`. `find`/`file_stat`/`undo`/`insert` were removed.

> **Linting on write**: this module does NOT lint - that's a **workspace** feature. If you want diagnostics on every write/edit, use the [workspace](workspace.md) module with `lint: true` instead.

---

## Actions (5)

| Tool Name | Action | Visible Params | Description |
|-----------|--------|----------------|-------------|
| `Read`   | `read`  | `file_path` (+ `offset`, `limit` for large files) | Read file with line numbers. Detects PDFs (`pages`) and images (base64 for vision). |
| `Write`  | `write` | `file_path`, `content` | Create or overwrite a file. Creates parent directories. |
| `Edit`   | `edit`  | `file_path`, `old_string`, `new_string` | Find-and-replace. `old_string` must be unique. 6-strategy fuzzy matching. |
| `Glob`   | `glob`  | `pattern` (+ optional `path`) | Find files by pattern (`**/*.py`). Sorted by mtime. |
| `Grep`   | `grep`  | `pattern` (+ optional `path`) | Regex search inside files. Powered by ripgrep. |

---

### Read - `file_path` + optional `offset`, `limit`

Read a file and return its content with line numbers. **Always Read before Edit** - the runtime tracks which files the agent has read and refuses Edit on large unread files (>500 bytes).

**Visible params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `file_path` | string | yes | Absolute path. Accepts alias `path`. |
| `offset` | int | no | 0-based line to start reading from (for large files). Alias: `start_line`. |
| `limit` | int | no | Number of lines to read. Alias: `end_line`. |

**Hidden params** (not in LLM schema): `encoding`, `pages` (PDF ranges like `"1-5"`), `pattern` (content search), `max_binary_size`.

**Returns:** content with line numbers, metadata (`size`, `lines`, `mtime`), and for images `metadata.image_data` (base64 for vision).

---

### Write - `file_path` + `content`

Create or overwrite a file. Creates parent directories automatically. Atomic (writes to temp then renames). After writing, the path is added to the "read" set so subsequent `Edit` works without a prior `Read`.

**Visible params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `file_path` | string | yes | Absolute path. Parent dirs are created. |
| `content` | string | yes | Full file content. |

**Hidden params:** `create_dirs` (default true), `encoding`, `atomic` (default true).

**Returns:** `{path, size, lines, operation: "create"|"update", bytes_written}` in `metadata`. No `lint` field (see Design Philosophy).

---

### Edit - `file_path` + `old_string` + `new_string`

Surgical find-and-replace. `old_string` must appear **exactly once** in the file. For insertion at a specific line, use the hidden `insert_at_line`.

**Visible params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `file_path` | string | yes | Absolute path. Must be `Read` first unless the file is <500 bytes or was just `Write`-created. |
| `old_string` | string | yes | Exact text to replace. Not needed when using `insert_at_line`. |
| `new_string` | string | yes | Replacement text. |

**Hidden params:** `replace_all` (replace every occurrence), `insert_at_line` (1-based, insert mode), `fuzzy_threshold` (0.85), `max_suggestions` (3), `encoding`.

**Fuzzy matching cascade** (all return positions in ORIGINAL content):
1. Exact match
2. Per-line trailing whitespace normalization
3. CRLF/LF normalization
4. Whitespace collapse
5. Indentation-agnostic (strip both sides)
6. Fuzzy block via `SequenceMatcher` ≥85%

Auto-reindents `new_string` when the matched `old_string` indentation differs. On failure, suggests up to 3 closest matches with line numbers.

**Returns:** `{path, size, lines, diff, insertions, deletions}` in `metadata`.

---

### Glob - `pattern` + optional `path`

Find files by glob pattern. Sorted by modification time (most recent first).

**Visible params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `pattern` | string | yes | Glob like `**/*.py`, `src/**/*.ts`, `*.md`. |
| `path` | string | no | Directory to search (default: cwd). |

**Hidden params:** `type` (`file`\|`dir`), `max_results` (200), `include_hidden`, `follow_symlinks`.

**Returns:** list of absolute paths, sorted by mtime.

---

### Grep - `pattern` + optional `path`

Regex search inside file contents. Delegates to ripgrep when available; falls back to Python.

**Visible params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `pattern` | string | yes | Regular expression. |
| `path` | string | no | File or directory to search (default: cwd). |

**Hidden params:** `glob` (filename filter), `context` (lines before/after), `type` (rg file-type), `recursive`, `max_results` (250), `case_sensitive`, `output_mode` (`content`\|`files_with_matches`\|`count`), `multiline`, `offset`.

**Returns:** matches in the chosen `output_mode`.

---

## Removed actions (use Bash)

These actions were removed in v2.0 - the shell module's `Bash` covers them:

| Removed | Use instead |
|---------|-------------|
| `ls`    | `Bash("ls ...")` |
| `mv`    | `Bash("mv ...")` |
| `cp`    | `Bash("cp ...")` |
| `rm`    | `Bash("rm ...")` |
| `mkdir` | `Bash("mkdir -p ...")` or just `Write` (auto-creates parents) |
| `insert` | `Edit` with hidden `insert_at_line` param |
| `find`  | `Glob` with optional `type` filter |
| `file_stat` | `Bash("stat ...")` |
| `undo`  | `Bash("git checkout ...")` |

---

## Workspace path resolution

Relative paths are resolved from `self.workspace`, **not** the process CWD. When session isolation is enabled (see [Workspace](workspace.md)), each session gets its own directory under `~/.digitorn/workspaces/{app_id}/{session_id}/`.

Shell module integrates Git Bash on Windows - paths like `/c/Users/...` are auto-converted to `C:/Users/...` before workspace checks.

---

## YAML configuration

```yaml
modules:
  filesystem:
    constraints:
      allowed_actions: [read, write, edit, glob, grep]  # restrict to subset
      allowed_paths:                                    # restrict path roots
        - "{{workspace}}/**"
        - "/tmp/**"
      denied_paths:
        - "**/.env*"
        - "**/node_modules/**"
      max_file_size: 10485760                           # 10 MB
      readonly: false                                   # block write/edit when true
```
Action subsets and path constraints are enforced at dispatch time.
