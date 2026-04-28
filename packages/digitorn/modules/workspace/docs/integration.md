# Workspace Module - Integration

## Dependencies

- **`preview` module** - required at runtime. The workspace is a thin facade
  over `preview.set_resource("files", …)` / `delete_resource` / `set_state`.
  Bootstrap wires `workspace._preview` directly (not via service_bus).

## How it plugs in

### 1. Declare both modules in `app.yaml`

```yaml
modules:
  preview: {}
  workspace:
    config:
      render_mode: react
      entry_file: src/App.tsx
      title: "My App"
      instructions: |
        (app-specific convention prompt - injected into the agent's tool docs)
```

### 2. Grant the actions

```yaml
capabilities:
  grant:
    - module: workspace
      actions: [write, read, edit, glob, grep, delete]
```

### 3. Client shell reads the `files` channel

```tsx
import { useWorkspaceFileJson, useWorkspaceFilesJsonByPrefix } from "./preview-sdk";

const progress = useWorkspaceFileJson<Progress>("_state/progress.json");
const nodes = useWorkspaceFilesJsonByPrefix<AgentNode>("_state/graph/nodes/");
```

The client never needs to know about `workspace` as a module - it just
reads the preview's `files` channel by path, exactly like it would read
any other resource channel.

## Lifecycle

1. **Bootstrap** - module instantiated, config stored, meta NOT yet published
   (the preview session doesn't exist yet).
2. **First `write`** - `_ensure_meta_published()` runs lazily:
   - Auto-detects `render_mode` from the first file's language if
     `config.render_mode == "auto"`.
   - Publishes `preview.set_state("workspace", {render_mode, entry_file, title})`.
3. **Subsequent writes/edits/deletes** - stream through `preview.set_resource`
   / `delete_resource` on the `files` channel. No state updates.
4. **`on_config_update`** - resets `_meta_published` so a re-activated app
   can re-publish with a new render_mode without restarting.

## Durable snapshot / checkpoint / fork

Because workspace mutations flow through `preview.set_resource`, they
benefit from the preview module's debounced persistence. A closed
session can be reopened - even after a daemon restart - and the
client sees every file exactly as it was left. See `docs/PREVIEW.md`
and `docs/FRONTEND_WORKSPACE_SNAPSHOT_PROMPT.md` for the full contract,
including `/workspace/export`, `/workspace/import`, `/workspace/fork`
endpoints and the `useWorkspaceSnapshot` React hook.

## Sharing across sub-agents

The workspace module is `isolation = "shared"` (one instance per daemon),
but its state is per-session (keyed by the current preview session id).
Sub-agents spawned inside the same session see the same files automatically
- no special wiring needed in `agent_spawn/runner.py`.

## Dynamic tool prompts

`WorkspaceModule.get_dynamic_tool_prompts()` returns a dict of FQN → prompt,
merged at system-prompt-build time by `context_builder/prompt.py`:

- Base prompts are defined in `_BASE_TOOL_PROMPTS` (class attribute).
- The per-app `instructions` block is prepended to each.
- The per-tool `tool_instructions: {write: "…"}` override, if present,
  replaces the base prompt for that tool.

This lets each app teach the agent its own file-path conventions without
baking anything into the module.

## Validation workflow (approve / reject / diff)

Every write lands with `validation: "pending"` by default. The user
(or a hook) is expected to stage it explicitly before the change is
treated as "shipped".

### Payload fields

Each `resource_patched` event on the `files` channel carries:

| field | meaning |
|---|---|
| `content` | current in-memory content (post-write) |
| `validation` | `"pending"` or `"approved"` |
| `insertions_pending` | **delta** vs the last-approved baseline (0 when no diff) |
| `deletions_pending` | **delta** vs the last-approved baseline |
| `baseline_lines` | line count of the last-approved snapshot |
| `total_insertions` / `total_deletions` | cumulative session totals |
| `unified_diff` | per-operation diff (edit only) |
| `git_status` | `untracked` / `unstaged` / `staged` / `conflict` / `committed` |
| `source` | `"user"` if written via PUT writeback, else absent |

`insertions_pending` / `deletions_pending` are **delta-vs-baseline**,
not cumulative. After `approve()` they reset to 0; after a
single-line edit they show `1` / `1`, not `N` / `N-1`.

### Endpoints

**Get file content + pending diff:**
```
GET /api/apps/{app_id}/sessions/{sid}/workspace/files/{path}?include_baseline=true
→ { path, payload: {content, validation, insertions_pending, …}, baseline, unified_diff_pending }
```

The `unified_diff_pending` is guaranteed well-formed (every line has a
`\n`, safe for `difflib.PatchSet.from_string()`).

**Approve the whole file:**
```
POST /api/apps/{app_id}/sessions/{sid}/workspace/files/approve
Body: {path}
```

**Reject (revert to baseline, or delete if first write was never approved):**
```
POST /api/apps/{app_id}/sessions/{sid}/workspace/files/reject
Body: {path}
```

**Partial stage (Cursor / VS Code-style):**
```
POST /api/apps/{app_id}/sessions/{sid}/workspace/files/approve-hunks
Body: {path, hunks: [0, 2]}   # indices OR 12-char hunk hashes
```
Applies only the selected hunks to the baseline, leaving the rest
pending.

```
POST /api/apps/{app_id}/sessions/{sid}/workspace/files/reject-hunks
Body: {path, hunks: ["a8f3bc0e", 2]}
```
Reverts only the selected hunks in the current content.

Hunks are identified by either their 0-based index in the current
`unified_diff_pending` OR by a stable 12-char SHA-256 of the hunk
header + body (use the hash if there's any risk of a concurrent agent
write between the user reading the diff and clicking approve).

**User writeback (manual edit / conflict resolution / drag-drop import):**
```
PUT /api/apps/{app_id}/sessions/{sid}/workspace/files/{path}
Body: {content, auto_approve?: bool, source?: "user"}
```
Writes the content, emits `resource_patched` with `source: "user"`.
`auto_approve: true` snapshots the content as baseline in one call.

**Commit approved files to git:**
```
POST /api/apps/{app_id}/sessions/{sid}/workspace/commit
Body: {message, files?: [paths] | null, push?: bool}
→ {commit_sha, branch, files_committed, pushed}
```
`files: null` commits every file whose `validation == "approved"`.
Workspace must already be a git repo (has a `.git` dir). `.digitorn/`
stays gitignored.

**Per-file approval history:**
```
GET /api/apps/{app_id}/sessions/{sid}/workspace/files/{path}/history
→ { revisions: [{revision, approved_at, approved_by, tokens_delta_ins, tokens_delta_del, bytes}, …] }
```
Each `approve()` appends one entry; revision bodies are persisted at
`{ws}/.digitorn/sessions/{sid}/baselines/{path}.history/rev-NNNN` for
diff-between-revisions support.

## `auto_approve` mode - bypass validation

For sandbox apps, trusted-agent pipelines, or CI flows where no human
review is needed:

```yaml
modules:
  workspace:
    config:
      auto_approve: true
```

Effect on every `WsWrite` / `WsEdit` (or PUT writeback):
- `validation` stays `"approved"`
- `insertions_pending` / `deletions_pending` are always `0`
- the current content is immediately snapshotted as the new baseline
- a history revision is still appended (`approved_by: "auto"`)

The approve/reject/approve-hunks endpoints remain callable but are
effectively no-ops in this mode. Use the per-call
`PUT /workspace/files/{path} {auto_approve: true}` to auto-approve a
single writeback without flipping the module-level flag.

## When NOT to use the workspace

Use the `filesystem` module instead when:

- You need to persist files to disk (exports, package artifacts, builds).
- You need access to files outside the session (user's actual project).
- You need a real directory tree with mkdir/mv semantics (the workspace is
  flat - paths are just identifiers on a key-value channel).

The builder app uses both: `workspace.write("app.yaml", …)` for the live
YAML panel, and `filesystem.write("./packages/<id>/app.yaml", …)` for the
on-disk package install artifact.
