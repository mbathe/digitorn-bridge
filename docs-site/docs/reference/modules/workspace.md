---
id: workspace
title: workspace Module
sidebar_label: workspace
description: Six file actions for live-canvas apps - in-memory virtual FS streamed to the client over Socket.IO.
---

# workspace

The **workspace** module is the agent's file API for live-app
canvases (Lovable-style React sandboxes, LaTeX editors,
slides, custom builders). Files live in memory, stream live
to the client over Socket.IO, optionally mirror to disk, and
optionally lint on every write.

| Property | Value |
|----------|-------|
| Module id | `workspace` |
| Action count | 6 (LLM-exposed) |
| Type | per-app instance, per-session state |
| Pip deps | none (stdlib). |
| Dependencies | wraps `preview` (transport) + `lsp` (diagnostics, optional) |

## The 6 actions

| Tool | FQN | Source | Visible params | Purpose |
|------|-----|--------|----------------|---------|
| `WsWrite` | `workspace.write` | `module.py` | `path`, `content` | Create / overwrite. |
| `WsRead` | `workspace.read` | `module.py` | `path` | Read. |
| `WsEdit` | `workspace.edit` | `module.py` | `path`, `old_string`, `new_string` | Surgical text replacement (same fuzzy cascade as `filesystem`). |
| `WsGlob` | `workspace.glob` | `module.py` | `pattern` | Pattern match. |
| `WsGrep` | `workspace.grep` | `module.py` | `pattern` | Content regex search. |
| `WsDelete` | `workspace.delete` | `module.py` | `path` | Remove. |

### Visible vs hidden params

| Action | Visible | Hidden |
|--------|---------|--------|
| `write` | `path`, `content` | - |
| `read` | `path` | `offset`, `limit` |
| `edit` | `path`, `old_string`, `new_string` | `replace_all`, `insert_at_line`, `fuzzy_threshold`, `max_suggestions` |
| `glob` | `pattern` | `sort_by` |
| `grep` | `pattern` | `glob`, `case_insensitive`, `multiline`, `before`, `after`, `max_results` |
| `delete` | `path` | - |

## Auto-detection of `render_mode`

When `render_mode: auto`, the daemon
picks the renderer from the first file's extension:

| Extension | Resolved render_mode |
|-----------|----------------------|
| `.tsx`, `.jsx` | `react` |
| `.tex` | `latex` |
| `.md` | `markdown` |
| `.html` | `html` |
| `slides.md` / `*.slides.md` | `slides` |
| anything else | `code` |

## Configuration

```yaml
tools:
  modules:
    workspace:
      config:
        render_mode: react             # auto | react | html | markdown | slides | code | latex | builder
        entry_file: src/App.tsx        # main file to render first
        title: "My App"
        sync_to_disk: false            # mirror writes to real filesystem (Lovable-style)
        sync_path: null                # fixed disk dir (overrides auto-isolation)
        lint: true                     # diagnostics on every write/edit
        auto_approve: false            # bypass validation; every write lands approved
        agent_root: ""                 # whitelist-style agent scope (see below)
        instructions: |                # prepended to all workspace tool prompts
          You are building a React app...
        tool_instructions:             # per-tool override
          write: "Custom write instructions..."
```

### `agent_root` - scope lock for attachments mode

When non-empty, every workspace path the agent tries to
touch must start with `agent_root`. Anything outside is
treated as hidden: `WsRead` returns "file not found", `WsGlob`
skips it, `WsGrep` ignores it. The SDK iframe and the HTTP
workspace routes (`/api/apps/.../workspace/files/...`) are
NOT affected, they keep seeing the full tree. This is not a
sandbox, it is a convenience guardrail to keep the agent
focused on the directory it should be reading from.

The canonical use is the chat attachments tool-mode
(see [`app.attachments_mode`](../../language/02-app-config.md#appattachments_mode---how-the-agent-sees-attached-files)):
attachments land under `attachments/<name>`, and
`agent_root: "attachments"` ensures the agent can read them
via `WsRead` but cannot reach app-private files via `..` or
absolute paths.

```yaml
tools:
  modules:
    workspace:
      config:
        agent_root: "attachments"   # agent locked to attachments/
        auto_approve: true          # uploads land pre-approved
        lint: false
```

With this config, `WsRead("attachments/report.pdf")` works,
`WsRead("config.json")` returns not-found even when the file
exists in the workspace.

### Top-level `ui.workspace:` block

Separate from `tools.modules.workspace.config`, the
**`ui.workspace:`** block at the top level is what the
client reads to pick a renderer (see
[Workspace & Preview](../../language/41-preview.md) +
[Client Manifest](../../language/44-client-manifest.md)):

```yaml
ui:
  workspace:
    render_mode: react
    entry_file: src/App.tsx
    title: "My App"
    # Layout + open-on-mount (added 2026-05-04 / 2026-05-14)
    position: right                # right|bottom|hidden|overlay
    width_pct: 50                  # 10..90 split ratio
    auto_open_on_first_tool: true  # open on first file write
    default_open: false            # open immediately on session mount
    # View routing — which tab opens, which tabs are reachable
    default_view: auto             # auto|code|preview|changes|activity
    hidden_views: []               # subset of [code, preview, changes, activity]
    # Preview iframe chrome (toolbar above the live preview)
    preview_chrome:
      enabled: true
      refresh: true
      open_in_new_tab: true
      viewport_toggle: false
      url_bar: auto                # auto|always|never
```

See [Client manifest → `ui.workspace`](../../language/44-client-manifest.md#uiworkspace---renderer-hint--layout)
for the per-field reference (defaults, valid values, when each
one fires).

The two blocks coexist - `tools.modules.workspace` enables
the actions for the agent; `ui.workspace` tells the client
how to display the resulting files. Both are needed for a
fully functional live workspace.

## File payload sent to preview

Every mutation publishes to the `files` channel of the
preview module:

```json
{
  "content": "...",
  "language": "tsx",
  "size": 1234,
  "lines": 42,
  "status": "modified",
  "operation": "edit",
  "insertions": 5,
  "deletions": 2,
  "total_insertions": 47,
  "total_deletions": 12,
  "diff": "...",
  "unified_diff": "...",
  "updated_at": 1776297401.5,
  "validation": "pending",
  "insertions_pending": 5,
  "deletions_pending": 2
}
```

| Field | Description |
|-------|-------------|
| `status` | `added` / `modified` / `deleted`. |
| `operation` | `write` / `edit` / `delete`. |
| `insertions` / `deletions` | Lines changed in the last op. |
| `total_insertions` / `total_deletions` | Cumulative since session start. |
| `unified_diff` | Well-formed (parseable by `difflib.PatchSet`). |
| `validation` | `pending` (default) / `approved` (after approve, or when `auto_approve: true`). |
| `insertions_pending` / `deletions_pending` | Delta vs the **last-approved baseline**, NOT cumulative - reset to 0 after `approve`. |

## Validation workflow

Every `WsWrite` / `WsEdit` ships with `validation: "pending"`
unless `auto_approve` is on. The daemon exposes a per-session
workspace surface with operations grouped as:

| Operation | Purpose |
|-----------|---------|
| Read summary | File list, render mode, entry file, dirty flag. |
| Read file (with `?include_baseline=true`) | Content + baseline + `unified_diff_pending`. |
| Read file history | Revision list (`revision`, `approved_at`, `approved_by`, `tokens_delta_ins/del`). |
| Code snapshot | File tree + metadata only (validation, language, lines, status, pending-diff flags). **Does not include content** - fetch each file individually. |
| Preview snapshot | Live preview state (resources, channels, events). |
| Changes diff | Diff vs baseline across the whole session - pending hunks per file. |
| Export / import / fork | Portable workspace dump + restore + new-session-from-export. |
| Approve / reject (whole file) | Stage = current content / revert to baseline (or delete if never approved). |
| Approve / reject hunks | Partial stage / revert by hunk index OR 12-char hash. |
| User writeback | Manual edit, conflict resolution, drag-drop. |
| Commit | `git add` + `git commit` over approved files. |
| Refresh git status | Refresh `git_status` flags on every tracked file. |

The exact route shapes are not documented publicly - the
[Python testing SDK](../client-sdks/python-testing.md) and
the [React Preview SDK](../../language/47-preview-sdk.md)
expose these operations as typed methods.

Hunks have stable 12-char SHA-256 ids (header + body) - the
client can approve by hash instead of index to survive races
with concurrent agent writes. The `approve-hunks`
implementation applies hunks in **reverse position order** so
earlier indices aren't perturbed by later length changes.

Baseline + history persist to:
```
{ws}/.digitorn/sessions/{sid}/baselines/{path}             # baseline
{ws}/.digitorn/sessions/{sid}/baselines/{path}.history/    # revisions
  rev-NNNN
  _index.json
```

Survives daemon restart.

## `auto_approve: true` - bypass validation

```yaml
config:
  auto_approve: true
```

Every write / edit lands with `validation: "approved"`,
pending counters always zero, baseline = current on each
mutation. For sandbox apps / trusted-agent pipelines / CI.
Per-call override via
`PUT /workspace/files/{path} {auto_approve: true}` for a
single writeback without flipping the module-level flag.

## `sync_to_disk: true` - mirror to real filesystem

When set, every workspace mutation is mirrored to disk:

| Op | Effect |
|----|--------|
| `write` / `edit` | Writes updated content to `{sync_dir}/{path}`. |
| `delete` | Removes the file from disk. |
| `read` | **Read-through** - if the file isn't in memory but exists on disk, loads it. |
| `glob` / `grep` | Scans disk for files not yet in memory, then searches the union. |

Replaces the need for a separate `filesystem` module in apps
that generate real code (Lovable-style sandboxes, React, LaTeX).

### sync_dir resolution order

1. `sync_path` in YAML - fixed, never overridden.
2. `ctx.workspace` set by the user (the user picked a project
   folder in the UI).
3. Auto-isolated:
   `~/.digitorn/workspaces/{app_id}/{session_id}/`.

This prevents concurrent sessions from clobbering each
other's files.

## Lint on write

When `lint: true` (default), every `write` / `edit` returns
diagnostics inline:

1. **LSP module** (when loaded) -
   `lsp.notify_change(path, content)` → real language server
   (texlab, pyright, ruff, eslint, ...).
2. **Built-in content validators** - JSON, YAML, TOML, Python
   syntax, LaTeX (unmatched braces + environments). Work
   in-memory, no external tools.

Diagnostics appear as `{lint: [{line, severity, message,
source}, ...]}`.

The agent never needs to call `lsp.diagnostics` separately.

## Bootstrap wiring

`bootstrap.py`:

- `workspace._preview = preview_module` - Socket.IO transport.
- `workspace._lsp = lsp_module` - diagnostics provider (when
  loaded).
- Top-level `ui.workspace:` block fields injected
  (`render_mode`, `entry_file`, `title`).

## Cross-references

- App-config block reference (`ui.workspace`):
  [App Configuration → ui](../../language/02-app-config.md#ui---display-layer-daemon-never-reads)
- Workspace + preview YAML reference:
  [Workspace & Preview](../../language/41-preview.md)
- Preview transport (every `Ws*` call goes through `preview`):
  [preview reference](preview.md)
- Filesystem module (real-FS direct access):
  [filesystem reference](filesystem.md)
