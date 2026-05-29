---
id: filesystem
title: filesystem Module
sidebar_label: filesystem
sidebar_position: 1
description: Five agent-optimised filesystem actions - Read, Write, Edit, Glob, Grep. Same surface as Claude Code.
---

# filesystem

Agent-optimised filesystem operations. **5 actions**, short
PascalCase tool names (matching Claude Code), minimal visible
params + hidden implementation details for reliability.

| Property | Value |
|----------|-------|
| Module id | `filesystem` |
| Version | `2.0.0` |
| Action count | 5 |
| Type | system |
| Pip deps | None (stdlib). Optional: `ripgrep` for faster grep. |

## Design philosophy

Inspired by Claude Code's tool surface:

1. **Minimal visible params** → fewer LLM mistakes. All
   non-essential params are hidden from the JSON schema sent
   to the model.
2. **Hidden params + smart defaults** → encoding,
   fuzzy matching, and recovery are taken care of without
   widening the schema the model sees.
3. **Safety-first** - `Edit` refuses to overwrite files that
   weren't `Read` first (when > 500 bytes).
4. **No legacy actions** - use `Bash` for `ls`, `mv`, `cp`,
   `rm`, `mkdir`, `stat`. `find` / `file_stat` / `undo` /
   `insert` were removed in v2.0.

> **Linting on write** is a **`workspace` module** feature -
> not `filesystem`. If you want LSP-driven diagnostics on
> every write, use the [workspace module](workspace.md) with
> `lint: true`.

## The 5 actions

| Tool | FQN | Visible params | Purpose |
|------|-----|----------------|---------|
| `Read` | `filesystem.read` | `file_path` (+ `offset`, `limit`) | Read file with line numbers. Detects PDFs (`pages`) + images (base64 for vision). |
| `Write` | `filesystem.write` | `file_path`, `content` | Create / overwrite. Creates parent directories. |
| `Edit` | `filesystem.edit` | `file_path`, `old_string`, `new_string` | Find-and-replace. `old_string` must be unique. 6-strategy fuzzy matching. |
| `Glob` | `filesystem.glob` | `pattern` (+ `path`) | Find files by pattern (`**/*.ts`). Sorted by mtime. |
| `Grep` | `filesystem.grep` | `pattern` (+ `path`) | Regex search inside files. Powered by ripgrep. |

### `Read` - `file_path` + optional `offset`, `limit`

| Param | Type | Required | Description |
|-------|------|:--------:|-------------|
| `file_path` | string | yes | Absolute path. Accepts alias `path`. |
| `offset` | int | no | 0-based line to start reading from |
| `limit` | int | no | Number of lines to read. |

Hidden params: `encoding`, `pages` (PDF ranges like `"1-5"`),
`pattern` (content search), `max_binary_size`.

Returns content with line numbers + metadata
(`size`, `lines`, `mtime`). For images,
`metadata.image_data` carries the base64 for vision models.

> **Read-before-Edit gate**: the runtime tracks which paths
> the agent has read this session and refuses `Edit` on
> unread files larger than 500 bytes. Files smaller than 500 bytes can
> be edited without prior read; files just-`Write`-created
> are auto-added to the read set.

### `Write` - `file_path` + `content`

| Param | Type | Required | Description |
|-------|------|:--------:|-------------|
| `file_path` | string | yes | Absolute path. Parent dirs are auto-created. |
| `content` | string | yes | Full file content. |

Hidden params: `create_dirs` (default `true`), `encoding`,
`atomic` (default `true` - temp + rename).

After writing, the path is added to `_read_files` so
subsequent `Edit` works without a prior `Read`.

Returns `{path, size, lines, operation: "create"|"update",
bytes_written}` in `metadata`.

### `Edit` - `file_path` + `old_string` + `new_string`

| Param | Type | Required | Description |
|-------|------|:--------:|-------------|
| `file_path` | string | yes | Must be `Read` first unless < 500 bytes or just `Write`-created. |
| `old_string` | string | yes | Exact text to replace (omit when using `insert_at_line`). |
| `new_string` | string | yes | Replacement text. |

Hidden params: `replace_all`, `insert_at_line` (1-based,
insert mode), `fuzzy_threshold` (default `0.85`),
`max_suggestions` (default `3`), `encoding`.

#### Fuzzy matching cascade

 All 6 strategies return positions in
the **original** content (not normalised):

1. Exact match.
2. Per-line trailing whitespace normalisation.
3. CRLF / LF normalisation.
4. Whitespace collapse.
5. Indentation-agnostic (strip both sides).
6. Fuzzy block via `difflib.SequenceMatcher` ≥ 0.85.

`_reindent_replacement` auto-adjusts indentation: if the LLM
sent 0-indent `old_string` but the file has 4-indent,
`new_string` is re-indented to match.

On failure: `find_closest_matches` suggests up to 3 matches
(>50 % similarity) with line numbers.

Returns `{path, size, lines, diff, insertions, deletions}`
in `metadata`.

### `Glob` - `pattern` + optional `path`

| Param | Type | Required | Description |
|-------|------|:--------:|-------------|
| `pattern` | string | yes | Glob (`**/*.ts`, `src/**/*.ts`, `*.md`). |
| `path` | string | no | Directory to search (default = workspace). |

Hidden params: `type` (`file` / `dir`), `max_results`
(default `200`), `include_hidden`, `follow_symlinks`.

Returns absolute paths sorted by mtime (newest first).

### `Grep` - `pattern` + optional `path`

| Param | Type | Required | Description |
|-------|------|:--------:|-------------|
| `pattern` | string | yes | Regular expression. |
| `path` | string | no | File or directory (default = workspace). |

Hidden params: `glob` (filename filter), `context` (lines
before / after), `type` (rg file-type), `recursive`,
`max_results` (default `250`), `case_sensitive`,
`output_mode` (`content` / `files_with_matches` / `count`),
`multiline`, `offset`.

Delegates to ripgrep when available; falls back to Python
otherwise.

## Removed actions (use Bash)

| Removed | Use instead |
|---------|-------------|
| `ls` | `Bash("ls ...")` |
| `mv` | `Bash("mv ...")` |
| `cp` | `Bash("cp ...")` |
| `rm` | `Bash("rm ...")` |
| `mkdir` | `Bash("mkdir -p ...")` (or just `Write` - auto-creates parents) |
| `insert` | `Edit` with the hidden `insert_at_line` param |
| `find` | `Glob` with optional `type` filter |
| `file_stat` | `Bash("stat ...")` |
| `undo` | `Bash("git checkout ...")` |

## Workspace path resolution

Relative paths are resolved from `self.workspace`, **not**
the process CWD. The workspace comes from `runtime.workdir`.

On Windows, Git Bash paths (`/c/Users/...`) are auto-converted
to Windows form (`C:/Users/...`) before the workspace check.

## Built-in validators (lint after write)

- JSON (`.json`, `.jsonc`), YAML
(`.yaml`, `.yml`), TOML (`.toml`), TypeScript (`.ts`).
The LSP module is tried first (ruff, eslint, ...); built-in
parsers are the fallback. Lint results appear as the `lint`
field in `Write` / `Edit` output **only when the LSP module
is loaded** - vanilla `filesystem` does not lint by default.

## Configuration

```yaml
tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, write, edit, glob, grep]  # restrict subset
        allowed_paths:
          - "{{workdir}}/**"
          - "/tmp/**"
        denied_paths:
          - "**/.env*"
          - "**/node_modules/**"
        max_file_size: 10485760                            # 10 MB
        readonly: false                                    # true → block write/edit
```

## Cross-references

- App-config block reference (`tools.modules.filesystem`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- Workspace module (in-memory virtual FS + lint + sync to
  disk): [workspace reference](workspace.md)
- Behavior engine (built-in `read_before_edit` rule):
  [Behavior Engine](../../language/43-behavior.md#the-14-built-in-rules)
- Sandbox Landlock guards (kernel-level path enforcement):
  [OS-Level Sandbox → Landlock](../../language/35-sandbox.md#1-landlock---filesystem)
