# Workspace Module

The **workspace** module is the agent's file API for apps with live previews.
It replaces the removed workbench system. Files live in memory, stream live
to the client via Socket.IO, and optionally mirror to disk.

## Actions (6 agent-facing)

| Tool Name | Module Action | Purpose |
|-----------|--------------|---------|
| `WsWrite` | `workspace.write` | Create or overwrite a file |
| `WsRead` | `workspace.read` | Read file content |
| `WsEdit` | `workspace.edit` | Surgical text replacement |
| `WsGlob` | `workspace.glob` | List files by pattern |
| `WsGrep` | `workspace.grep` | Search file contents |
| `WsDelete` | `workspace.delete` | Remove a file |

### Visible vs hidden params

The LLM sees only essential params; advanced params are hidden:

| Action | Visible | Hidden |
|--------|---------|--------|
| `write` | `path`, `content` | — |
| `read` | `path` | `offset`, `limit` |
| `edit` | `path`, `old_string`, `new_string` | `replace_all`, `insert_at_line`, `fuzzy_threshold`, `max_suggestions` |
| `glob` | `pattern` | `sort_by` |
| `grep` | `pattern` | `glob`, `case_insensitive`, `multiline`, `before`, `after`, `max_results` |
| `delete` | `path` | — |

## Configuration

```yaml
modules:
  workspace:
    config:
      render_mode: react         # react | builder | latex | slides | html | markdown | code | auto
      entry_file: src/App.tsx    # main file the client renders first
      title: "My App"            # optional display title
      sync_to_disk: false        # mirror writes to real filesystem
      sync_path: null            # fixed disk path (overrides auto-isolation)
      lint: true                 # run diagnostics on write/edit
      instructions: |            # prepended to all workspace tool prompts
        You are building a React app...
      tool_instructions:         # per-tool override
        write: "Custom write instructions..."
```
### Top-level `workspace:` block

Separate from `modules.workspace.config`, this block is read by the Flutter
client to know what renderer to use:

```yaml
workspace:
  render_mode: react
  entry_file: src/App.tsx
  title: "My App"
```
## File tracking metadata

Every file payload sent to the preview channel includes change tracking:

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
  "updated_at": 1776297401.5
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | `"added" \| "modified" \| "deleted"` | File status |
| `operation` | `"write" \| "edit" \| "delete"` | Last operation |
| `insertions` | int | Lines added in last op |
| `deletions` | int | Lines removed in last op |
| `total_insertions` | int | Cumulative since session start |
| `total_deletions` | int | Cumulative since session start |
| `diff` | string | Short diff (edit only) |
| `unified_diff` | string | Full unified diff (edit only) |
| `updated_at` | float | Unix timestamp |

## Session isolation

When `sync_to_disk: true`, files are mirrored to disk with isolation per session:

1. **`sync_path` in YAML** → fixed path, never overridden
2. **`ctx.workspace` set by user** → the user selected a project folder
3. **Auto-isolated** → `~/.digitorn/workspaces/{app_id}/{session_id}/`

This prevents concurrent sessions from overwriting each other's files.

## Read-through from disk

When `sync_to_disk: true` and a file exists on disk but not in memory:
- `WsRead` loads it transparently
- `WsGlob` and `WsGrep` scan disk files
- This makes the workspace compatible with pre-existing project files

## Lint

When `lint: true`, every `write` and `edit` returns diagnostics:
1. **LSP module** (if loaded) — real language servers (ruff, eslint, texlab, etc.)
2. **Built-in parsers** — JSON, YAML, TOML, Python syntax, LaTeX

Diagnostics appear in the tool response as `{"diagnostics": [{"line", "severity", "message", "source"}, ...]}`.

## Bootstrap wiring

In `core/runtime/bootstrap.py`:
- `workspace._preview = preview_module` — the Socket.IO transport
- `workspace._lsp = lsp_module` — diagnostics provider (if loaded)
- Top-level `workspace:` block injected as config fields

## Connection to the preview

Every workspace mutation publishes to the preview channel:

```
WsWrite("src/App.tsx", code)
  → workspace stores in memory
  → workspace calls preview.set_resource("files", "src/App.tsx", payload)
  → Socket.IO emits preview:resource_set
  → Client SDK useFiles() hook updates
  → React app renders new code
```

The agent never touches preview directly — workspace is the API.

## render_mode values

| Mode | Client behavior |
|------|----------------|
| `react` | Render React components in WebView |
| `builder` | n8n-style flow canvas (digitorn-builder) |
| `html` | Raw HTML in iframe |
| `markdown` | Native markdown rendering |
| `slides` | Slide deck (each `.md` = slide) |
| `code` | Syntax highlighting only |
| `latex` | LaTeX → PDF rendering |
| `auto` | Detect from first file extension |

## Capabilities grant

```yaml
capabilities:
  default_policy: block
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]
```
## Example: Lovable-style React sandbox

```yaml
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
        Write the main component to src/App.tsx.
  preview: {}

agents:
  - id: coder
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config: { api_key: "claude-code" }
    system_prompt: |
      You are a React code generator. Use workspace tools
      to write files. The user sees your code rendered live.

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
```