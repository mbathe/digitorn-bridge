# Workspace Module — Action Reference

Six universal primitives over the preview's `files` channel. Every mutation
streams live to the client via `preview:resource_set` / `resource_deleted`
events, so the UI updates in real time.

The agent sees these as `WsWrite`, `WsRead`, `WsEdit`, `WsGlob`, `WsGrep`,
`WsDelete` (short Claude-Code-style names); the FQN form is `workspace.*`.

---

## write

Create or overwrite a file. Auto-creates intermediate "directories" (they
are virtual — just path prefixes). Publishes workspace metadata to the
preview state on the very first write of a session.

**Permissions:** none (workspace is session-scoped)
**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | string | yes | — | Forward-slash path relative to the workspace root |
| `content` | string | yes | — | Full file content |

### Returns

```json
{"path": "src/App.tsx", "language": "typescript", "size": 1284, "lines": 42}
```

### Example

```python
workspace.write(
    path="_state/graph/nodes/memory.json",
    content='{"id": "memory", "label": "memory", "x": 200, "y": 120, "status": "idle"}',
)
```

---

## read

Read a file from the workspace. Returns content with line numbers (like
the filesystem `read` action) for precise referencing in follow-up edits.

**Permissions:** none
**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | string | yes | — | Path to read |

### Returns

```json
{"path": "src/App.tsx", "content": "1\timport ...", "language": "typescript",
 "size": 1284, "lines": 42}
```

### Errors

- `File not found: <path>` — no file at that path in the current session.

---

## edit

Surgical find-and-replace in an existing file. Fails if the `old_string`
is not found verbatim, or if it appears more than once (ambiguous). Prefer
this over a full `write` when changing a single field — the diff is small
and UI transitions stay smooth.

**Permissions:** none
**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | string | yes | — | Path to edit |
| `old_string` | string | yes | — | Exact text to find (must be unique) |
| `new_string` | string | yes | — | Replacement text |

### Example

```python
workspace.edit(
    path="_state/graph/nodes/memory.json",
    old_string='"status": "idle"',
    new_string='"status": "active"',
)
```

### Errors

- `File not found: <path>`
- `old_string not found in <path>`
- `old_string appears N times in <path> — make it more specific`

---

## glob

List every file whose path matches the given pattern. Supports standard
glob syntax (`*`, `**`, `?`, `[…]`). Useful for enumerating convention
folders like `_state/graph/nodes/*.json`.

**Permissions:** none
**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `pattern` | string | yes | — | Glob pattern relative to workspace root |

### Returns

```json
{"matches": ["_state/graph/nodes/memory.json", "_state/graph/nodes/http.json"], "count": 2}
```

---

## grep

Search file contents for a regex pattern. Returns matching lines with
their file path and line number.

**Permissions:** none
**Risk level:** Low

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `pattern` | string | yes | — | Regex pattern |
| `path` | string | no | `""` | Limit search to files under this prefix |

### Returns

```json
{"matches": [{"path": "app.yaml", "line": 42, "text": "temperature: 0.7"}], "count": 1}
```

---

## delete

Remove a file from the workspace. Publishes a `preview:resource_deleted`
event so the client can drop it from its rendered state.

**Permissions:** none
**Risk level:** Low (session-scoped, not on disk)

### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `path` | string | yes | — | Path to remove |

### Errors

- `File not found: <path>`

---

## Convention layouts (examples)

The workspace module is deliberately layout-agnostic. Each app type defines
its own conventions via `workspace.config.instructions`, and the client shell
renders accordingly.

### Digitorn Builder (node/edge canvas)

```
app.yaml                                  ← YAML being built
_state/progress.json                      ← 8-step pipeline status
_state/compile.json                       ← last compile result
_state/spec.json                          ← interview answers
_state/deploy.json                        ← deploy status
_state/graph/nodes/<id>.json              ← React Flow nodes
_state/graph/edges/<id>.json              ← React Flow edges
```

### React sandbox (Lovable-style)

```
src/App.tsx                               ← entry
src/components/<Name>.tsx
src/lib/*.ts
package.json
```

### Slide deck

```
slides/01.md
slides/02.md
...
theme.json
```

### LaTeX editor

```
main.tex
refs.bib
_state/compile.json                       ← pdflatex result
```

All of these use the same 6 primitives. No app-specific module code.
