---
id: module-concept-workspace
title: "workspace module — overview"
type: module-concept
module: workspace
isolation: shared
keywords: [workspace, workspace-module, write, read, edit, glob, grep, delete]
version: 1.0.0
---

# `workspace` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 6 visible, 7 internal

## Description (from class docstring)

Workspace Module — universal virtual filesystem for live-preview apps.

The agent sees the same 6 tools it knows from the real filesystem:
``Write``, ``Read``, ``Edit``, ``Glob``, ``Grep``, ``Delete``.
It doesn't know (or care) that the files live in memory and stream
in real time to the connected client via Socket.IO.

Under the hood every mutation publishes a ``preview:resource_set``
(or ``preview:resource_patched`` / ``preview:resource_deleted``) event
on the ``files`` channel. The client (Flutter / React) decides how to
render based on file extensions and the ``workspace`` state metadata.

Multi-step editing (slides, chapters, components) works naturally:
each file is a resource in the ``files`` channel. Write slide-01.md,
then slide-02.md, then edit slide-01.md — the client sees each
mutation in real time and reacts accordingly.

The module requires ``preview`` to be loaded in the same app.

Config (app.yaml)::

    modules:
      workspace:
        config:
          render_mode: react     # react | latex | slides | html | markdown | auto
          entry_file: src/App.tsx # main file the client should render first
          title: My App          # optional display title

The config is published as ``preview.set_state("workspace", {...})``
so the client shell can read ``usePreviewState("workspace")`` and
activate the correct renderer without any backend changes.

> Class-level summary: Virtual workspace — filesystem-like API that streams to the client.

## Configuration

Set under `modules.workspace.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon at module init time. Do NOT set manually in YAML — the daemon resolves it from the app's workspace/workspace_mode config. |
| `render_mode` | str |  | `'auto'` | How the client should render files. Values: react, latex, slides, html, markdown, code, auto. When 'auto', detected from the first file written. |
| `entry_file` | str \| None |  | `None` | Main file the client renders first (e.g. src/App.tsx, main.tex). |
| `title` | str \| None |  | `None` | Optional display title for the workspace. |
| `sync_to_disk` | bool |  | `True` | When true (default), every write/edit/delete is mirrored to a disk directory — either the user-picked workspace (when ``workspace_path`` is passed at session creation) or an auto-isolated per-sessi... |
| `sync_path` | str \| None |  | `None` | Directory on disk where files are synced. Relative paths are resolved from the app's workspace dir. Defaults to the app's workspace dir if sync_to_disk is true but no path is given. |
| `lint` | bool |  | `True` | When true, every write/edit runs diagnostics on the file and returns errors/warnings inline. Uses the LSP module if loaded, otherwise falls back to built-in validators (JSON, YAML, TOML, Python syn... |
| `instructions` | str \| None |  | `None` | App-specific instructions prepended to ALL workspace tool prompts. Tells the agent what kind of files to write (React, LaTeX, slides…). |
| `tool_instructions` | dict[str, str] \| None |  | `None` | Per-tool instruction overrides. Keys are action names: write, read, edit, glob, grep, delete. Each value replaces the base tool_prompt for that action. |
| `auto_approve` | bool |  | `False` | When true, every write/edit is implicitly approved — the baseline becomes the file's current content on each write, ``validation`` stays ``approved`` and pending counters are always zero. No human ... |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `write` | `WsWrite` |  | low | Create or overwrite a file. Streams live to the client. |
| `read` | `WsRead` |  | low | Read a file from the workspace. |
| `edit` | `WsEdit` |  | low | Surgical text replacement in an existing file. |
| `glob` | `WsGlob` |  | low | Find files by name pattern (e.g. **/*.tsx, slides/*.md). |
| `grep` | `WsGrep` |  | low | Search file contents by regex pattern. |
| `delete` | `WsDelete` |  | low | Delete a file from the workspace. |
| `approve_file` | `WorkspaceApproveFile` | ✓ | low | Mark a file as approved — its current content becomes the new baseline. |
| `reject_file` | `WorkspaceRejectFile` | ✓ | low | Reject the pending changes — revert the file to its last-approved baseline (or delete it if never approved). |
| `approve_file_hunks` | `WorkspaceApproveFileHunks` | ✓ | low | Approve only specific hunks of a file (partial staging). |
| `reject_file_hunks` | `WorkspaceRejectFileHunks` | ✓ | low | Reject only specific hunks of a file (partial revert). |
| `writeback_file` | `WorkspaceWritebackFile` | ✓ | low | User-side writeback (manual edit or conflict resolution). |
| `commit_session` | `WorkspaceCommitSession` | ✓ | medium | Commit the session workspace to git. |
| `git_status` | `WorkspaceGitStatus` | ✓ | low | Refresh git_status for every tracked workspace file. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {workspace: [write, read, edit, glob, grep]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/workspace-*.md`.
