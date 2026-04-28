# Workspace Module

Virtual filesystem that streams live to the client preview. The agent writes
files with 6 universal primitives and the client renders them - whatever the
app type (React sandbox, LaTeX editor, slide deck, builder canvas, …).

## Why

A typed per-app-type API (`push_node`, `push_slide`, `push_cell`, …) forces
every new live-preview app to invent its own protocol, and every agent to
learn a new vocabulary. The workspace module replaces all of them with one
generic filesystem over the preview's `files` channel.

The agent learns **six** primitives once; per-app conventions are expressed as
**file-path layouts** taught via a dynamic tool prompt injected from the app's
`workspace.config.instructions`.

## Actions (6)

| Action | Description |
|---|---|
| `write` | Create or overwrite a file. Auto-creates parents. |
| `read` | Read a file with line numbers. |
| `edit` | Surgical find-and-replace in an existing file. |
| `glob` | List files matching a glob pattern. |
| `grep` | Search file contents (regex). |
| `delete` | Remove a file. |

Every mutation streams via the preview module's `set_resource("files", …)`
event so the client updates in real time without polling.

## How apps plug in

In `app.yaml`:

```yaml
modules:
  preview: {}
  workspace:
    config:
      render_mode: builder        # react | latex | slides | html | markdown | code | auto
      entry_file: app.yaml        # main file to show in the preview
      title: "Digitorn Builder"
      instructions: |
        You are building a Digitorn app. Write the YAML to "app.yaml" and
        use these convention folders to drive the client:
          _state/progress.json       → pipeline step
          _state/graph/nodes/*.json  → React Flow nodes
          _state/graph/edges/*.json  → React Flow edges

capabilities:
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]
```

The `instructions` block is merged into the agent's per-tool prompts at
runtime via `get_dynamic_tool_prompts()`, so no static tool_prompt is baked
into the module.

## Render modes

On first `write`, the module publishes metadata to
`preview.set_state("workspace", {render_mode, entry_file, title})`. The client
shell reads this to pick the renderer:

| `render_mode` | First-file auto-detect | Default entry |
|---|---|---|
| `react` | `.tsx`, `.jsx`, `.ts`, `.js` | `src/App.tsx` |
| `latex` | `.tex`, `.bib` | `main.tex` |
| `slides` | - | `slides/01.md` |
| `html` | `.html`, `.css` | `index.html` |
| `markdown` | `.md` | `README.md` |
| `code` | `.py`, `.rs`, `.go`, … | first file |
| `builder` | - | app-defined |
| `auto` | pick from first file's language | `_LANG_TO_RENDER` |

## Safety

- All paths are normalized and kept within a virtual root - no directory
  traversal outside the workspace channel.
- `delete` is the only destructive action; everything else is append/overwrite.
- Files live only in the preview session, not on disk. For physical disk
  artifacts (packaging, exports), use the `filesystem` module instead.

See [`docs/actions.md`](docs/actions.md) for the full action reference.
