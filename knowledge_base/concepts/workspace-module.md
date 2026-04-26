---
id: workspace-module
title: "Workspace module — the agent's file API for live apps"
type: concept
keywords: [workspace, files, write, read, edit, glob, grep, delete, sync, disk, preview, render_mode, entry_file, lint, session, isolation]
related: [preview-module, preview-sdk, bundle-namespaces]
source: packages/digitorn/modules/workspace/module.py
---

# Workspace module — the agent's file API for live apps

## What it is

The workspace module gives agents 6 file operations (write, read,
edit, glob, grep, delete) that work on an **in-memory virtual
filesystem**. Every mutation is streamed to the client preview in
real time via the preview module. Optionally, mutations are also
synced to the real filesystem on disk.

Tool names: **WsWrite, WsRead, WsEdit, WsGlob, WsGrep, WsDelete**

## How it connects to the preview

```
Agent calls WsWrite("src/App.tsx", code)
  → workspace stores file in memory
  → workspace calls preview.set_resource("files", "src/App.tsx", {content, language, ...})
  → Socket.IO emits preview:resource_set to the session room
  → Client SDK's useFiles() hook updates
  → Preview re-renders with new code
```

The agent never calls preview directly. Workspace is the API.

## YAML configuration

```yaml
modules:
  workspace:
    config:
      render_mode: react      # react | builder | latex | slides | html | markdown | code | auto
      entry_file: src/App.tsx  # main file the client renders first
      title: My App            # display title
      sync_to_disk: false      # mirror writes to real filesystem
      sync_path: null          # fixed disk path (overrides auto-isolation)
      lint: true               # run diagnostics on every write/edit
      instructions: |          # prepended to all workspace tool prompts
        You are building a React app...
      tool_instructions:       # per-tool override
        write: "Custom write instructions..."
```

## Workspace top-level block

Separate from `modules.workspace.config`, this block is read by
the Flutter client to know what renderer to use:

```yaml
workspace:
  render_mode: react
  entry_file: src/App.tsx
  title: "My App"
```

## Action parameters

| Action | Visible params | Hidden params |
|--------|---------------|---------------|
| write  | path, content | — |
| read   | path | offset, limit |
| edit   | path, old_string, new_string | replace_all, insert_at_line, fuzzy_threshold |
| glob   | pattern | sort_by |
| grep   | pattern | glob, case_insensitive, multiline, before, after, max_results |
| delete | path | — |

## sync_to_disk — disk isolation

When `sync_to_disk: true`, every workspace mutation is mirrored to
disk. The target directory is resolved in this order:

1. **sync_path from YAML** — fixed path, never overridden. Use this
   when the app always works in a specific directory.

2. **ctx.workspace (user-selected folder)** — the user chose a
   project folder when creating the session. Used by coding apps
   where the user says "work on /home/me/my-project".

3. **Auto-isolated per session** — if neither sync_path nor
   user-selected workspace exists, files go to:
   ```
   ~/.digitorn/workspaces/{app_id}/{session_id}/
   ```
   This prevents concurrent sessions from overwriting each other.
   Perfect for Lovable-style apps where each conversation generates
   independent code.

### Read-through from disk

When `sync_to_disk: true` and a file exists on disk but not in
memory (e.g. pre-existing project files), `WsRead` loads it
transparently. `WsGlob` and `WsGrep` also scan disk files.

## lint — built-in diagnostics

When `lint: true` (default), every `write` and `edit` returns
diagnostics inline in the tool response:

1. **LSP module** (if loaded) — real language server (ruff, eslint, etc.)
2. **Built-in parsers** — JSON, YAML, TOML, Python syntax, LaTeX

The agent sees errors immediately and can fix them.

## render_mode values

| Mode | What it means |
|------|---------------|
| `react` | Client renders React components (Lovable-style) |
| `builder` | Client renders a builder canvas with nodes/edges (n8n-style) |
| `html` | Client renders raw HTML in an iframe |
| `markdown` | Client renders markdown |
| `latex` | Client renders LaTeX → PDF |
| `slides` | Client renders slide deck |
| `code` | Client shows code with syntax highlighting |
| `auto` | Client picks based on file extensions |

## Capabilities grant

```yaml
capabilities:
  default_policy: block
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]
```

## Example: Lovable-style React code generator

```yaml compile=skip
app:
  app_id: react-sandbox
  name: "React Sandbox"

modules:
  workspace:
    config:
      render_mode: react
      entry_file: src/App.tsx
      sync_to_disk: true
      lint: true
      instructions: |
        Generate React + Tailwind code.
        Write to src/App.tsx for the main component.
  preview: {}

agents:
  - id: coder
    brain:
      provider: anthropic
      model: claude-haiku-4-5-20251001
      config: { api_key: "claude-code" }
    system_prompt: |
      You are a React code generator. Use workspace tools to
      write files. The user sees your code rendered live.

execution:
  mode: conversation
  entry_agent: coder

capabilities:
  default_policy: block
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]

workspace:
  render_mode: react
  entry_file: src/App.tsx

preview:
  enabled: false
```
