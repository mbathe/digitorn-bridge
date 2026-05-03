# web_preview

Session-scoped iframe preview attachments. The agent points the
`/api/apps/{app_id}/preview/` endpoint at one of:

- **A running dev server** (HTTP proxy) — `PreviewProxy(port=5173, name="default")`
- **A static directory in the session workspace** — `PreviewStatic(path="dist", name="default")`

Both attachments are scoped to the current session; two sessions of the
same app can have completely independent previews. Multiple attachments
per session are supported via the `name` field, e.g. `name="frontend"`
and `name="backend"` simultaneously.

## Tools exposed to the LLM

| Short name      | FQN                  | Purpose                                                      |
|-----------------|----------------------|--------------------------------------------------------------|
| `PreviewProxy`  | `web_preview.proxy`  | Attach the iframe to a running dev server on a TCP port.      |
| `PreviewStatic` | `web_preview.static` | Attach the iframe to a directory in the session workspace.   |
| `PreviewDetach` | `web_preview.detach` | Drop a previously-registered attachment by name.             |
| `PreviewList`   | `web_preview.list`   | List the active attachments for the current session.         |

## Typical agent flows

**Live coding (dev server):**

```
Bash(command="cd web && npm run dev", run_in_background=true)
# wait until output shows "Local: http://localhost:5173/"
PreviewProxy(port=5173)
```

**Production build + static serve:**

```
Bash(command="cd web && npm run build")
PreviewStatic(path="web/dist")
```

**Multi-server app:**

```
Bash(command="cd backend && uvicorn main:app --port 8001", run_in_background=true)
Bash(command="cd web && npm run dev", run_in_background=true)
PreviewProxy(port=8001, name="api")
PreviewProxy(port=5173, name="web")
```

## Daemon-side wiring

The daemon HTTP route `_proxy_preview_http` reads
`module.get_attachment(session_id, name)` directly. The route does NOT
go through a tool call — it just consults the in-memory registry. The
session ID comes from the iframe URL's `?session_id=` query param.

The `web_preview` module is `isolation = "shared"` (one instance for
the whole daemon) — the per-session distinction is encoded in the
attachment key `(session_id, name)`. Cleanup happens in
`cleanup_session(session_id)` which the session manager calls on
session end.
