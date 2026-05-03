# web_preview actions

## PreviewProxy — `web_preview.proxy`

Attach the iframe preview to a running dev server.

| Param          | Type    | Default     | Description |
|----------------|---------|-------------|-------------|
| `port`         | int     | required    | TCP port the dev server is listening on. |
| `name`         | string  | `"default"` | Attachment name. Multi-attach by name (frontend / backend / etc). |
| `host`         | string  | `127.0.0.1` | (hidden) Host the dev server is bound to. |
| `health_check` | bool    | `true`      | (hidden) Quick TCP probe before registering. Logs a warning on failure but registers anyway. |

The agent is responsible for spawning the dev server itself
(`Bash(command="npm run dev", run_in_background=true)`) and for
resolving port conflicts. The daemon does NOT manage the dev-server
lifecycle.

## PreviewStatic — `web_preview.static`

Serve a directory inside the session workspace as static files.

| Param         | Type   | Default        | Description |
|---------------|--------|----------------|-------------|
| `path`        | string | `"dist"`       | Workspace-relative path to serve. Must exist on disk under the session's workspace dir. |
| `name`        | string | `"default"`    | Attachment name. |
| `index_file`  | string | `"index.html"` | (hidden) File served when the request path is empty or `/`. |

`workspace.sync_to_disk` must be `true` (the default) for the resolved
path to exist on disk. Each request reads from disk live, so rebuilding
the artifact is reflected on the next page load with no re-attach
needed.

## PreviewDetach — `web_preview.detach`

| Param | Type   | Default     | Description |
|-------|--------|-------------|-------------|
| `name`| string | `"default"` | Attachment name to drop. |

## PreviewList — `web_preview.list`

No params. Returns `{ attachments: [...], count: N }` for the current
session.

## Routing semantics (daemon-side)

`/api/apps/{app_id}/preview/?session_id=X[&name=Y]` resolution order:

1. `(session_id=X, name=Y or "default")` is in the registry → serve via
   the attachment (proxy or static depending on type).
2. The app ships a `web/dist/` directory at its install dir → serve
   those files (declarative case, no LLM action).
3. `404 Not Found`.

## Session cleanup

`cleanup_session(session_id)` is called by the session manager when a
session ends. All attachments owned by that session are dropped. The
agent's background bash tasks are killed by `shell.cleanup_session` —
the two cleanups are orthogonal but both fire at session end, so the
order doesn't matter.
