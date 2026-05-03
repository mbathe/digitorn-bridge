# Agent With Preview — minimal SDK example

A 30-line app YAML that gives an agent the three modules it needs to
build any web preview from scratch:

| Module | Why |
|---|---|
| `workspace` | Per-session on-disk workspace, file CRUD tools (`WsWrite` / `WsRead` / `WsEdit` / `WsGlob` / `WsGrep` / `WsDelete`). |
| `shell` | `Bash` to install deps, run builds, spawn dev servers in background. |
| `web_preview` | `PreviewProxy` / `PreviewStatic` to wire the iframe in the user's Workspace panel. |

## Deploy

```bash
digitorn dev deploy examples/agent-with-preview/app.yaml
```

Open the app in the web client → choose a workspace dir → start
chatting. The 3 quick prompts cover the canonical flows:

- **Static landing page** → agent writes one HTML file, `PreviewStatic` it.
- **Vite + React app** → agent scaffolds + `npm install` + `npm run dev` + `PreviewProxy`.
- **Build + serve** → agent runs `npm run build`, `PreviewStatic dist`.

## What you (as a developer using the SDK) need to know

### Pick your mode

| Mode | When | Tool | Resource cost |
|---|---|---|---|
| **PreviewStatic** | App is built / read-only / production-grade | After `npm run build`: `PreviewStatic(path="dist")` | 0 process, 0 port |
| **PreviewProxy** | Active iteration with HMR | Spawn dev server: `Bash(run_in_background=true)`; then `PreviewProxy(port=N, bash_task_id=<task_id>)` | One Node process per attached session |
| **Declarative** | App ships a built `web/dist/` | (no LLM action; daemon serves automatically) | 0 process, 0 port |

The `bash_task_id` parameter is **important** for `PreviewProxy`: the
daemon's idle reaper kills the dev server after 30 min of no traffic,
preventing leaked processes when users close their tabs.

### Cheat sheet: what works, what to avoid

Things the runtime assumes but the daemon won't enforce for you:

- **Dev server must serve at `/`**, not `/api/apps/{id}/preview/`. The
  iframe URL the daemon emits is the *root* of your dev server. If
  your framework injects a base path (`next.config.js → basePath`,
  Vite `base: "/sub/"`), drop it for the preview build.
- **Pick a "weird" port.** Avoid anything `< 1024` (privileged on
  Linux), avoid the daemon (`8000`), the Next/Vite defaults agents
  routinely collide on (`3000`, `5173`), and anything you already
  bind elsewhere. `4001`, `4711`, `5234`, `8765` are all fine.
- **Always pass `bash_task_id`** to `PreviewProxy`. Without it the
  reaper can't kill the spawned dev server when the session goes
  idle, and the Node process stays alive until the host runs out of
  RAM. With it you get free GC after 30 min of no traffic.
- **HMR works on direct-connect** (the daemon is out of the hot
  path), but Next-with-turbopack and Vite both expect their HMR
  WebSocket to live at the *same origin* as the page. With cloud
  preview (`preview-{port}.your-domain`) that's true; with same-host
  loopback (`localhost:{port}`) that's also true. The only setup
  that breaks HMR is putting another reverse-proxy in front that
  strips `Upgrade: websocket` headers — see
  `docs/cloud-deployment/PREVIEW_CLOUD_DEPLOYMENT.md` for the nginx
  / Caddy snippets that get this right.
- **Static for prod, Proxy for iteration.** Proxy keeps a Node
  process per attached session (~150 MB RAM each); on a host with
  100 concurrent users that's `5 × 100 = 500` processes max → 75 GB.
  After the agent finishes editing, ask it to `npm run build` and
  switch the attachment to `PreviewStatic(path="dist")`: zero
  process, zero port, served straight from disk.
- **First render is cold.** The dev server takes a few seconds to
  bind after `Bash(run_in_background=true)`. The frontend already
  retries with exponential backoff (1s, 2s, 4s, 8s, 16s) before
  showing a manual-retry button — your agent does NOT need to
  `sleep` between spawning and `PreviewProxy(...)`.
- **One attachment per `name`.** Reattaching with the same `name`
  silently replaces the old one (no quota hit). Reattaching with a
  *different* `name` consumes a slot — call `PreviewDetach(name="…")`
  first if you're at the limit.

### Multi-attach (frontend + backend)

```python
PreviewProxy(port=5173, name="frontend", bash_task_id="t1")
PreviewProxy(port=8001, name="backend",  bash_task_id="t2")
```

The Workspace panel can show one at a time; clients select which via
`?name=frontend` in the iframe URL.

### Limits per session / per user

- Max **5** attachments per session
- Max **20** attachments per user (across all sessions)

Refusing further attachments returns a clear error to the agent
(suggesting `PreviewDetach` first).

### Cloud deployment

For a multi-tenant cloud, configure `web_preview.public_url_template`
in the daemon settings (default works for local dev with loopback):

```yaml
# ~/.digitorn/config.yaml
web_preview:
  public_url_template: "https://preview-{port}.digitorn.cloud"
```

Then set up wildcard DNS + an edge proxy (nginx / Caddy / Traefik)
that routes `preview-{port}.digitorn.cloud` to `127.0.0.1:{port}` on
the daemon host. See `docs/PREVIEW_CLOUD_DEPLOYMENT.md`.

### Operational health check

```bash
curl https://daemon.example.com/health/web_preview
```

Returns:

```json
{
  "status": "ok",
  "module_id": "web_preview",
  "version": "1.0.0",
  "count": 12,
  "by_type": {"proxy": 4, "static": 8},
  "by_user": {"u1": 3, "u2": 4, "anonymous": 5},
  "session_count": 7,
  "oldest_age_seconds": 3421.5,
  "oldest_idle_seconds": 1287.3,
  "limits": {
    "max_per_session": 5,
    "max_per_user": 20,
    "idle_reap_after_seconds": 1800
  }
}
```

Useful to grep during a launch or wire into Prometheus / Grafana.

## Required credential

The default agent uses DeepSeek; swap the `brain.provider` block in
`app.yaml` for any other provider you have a credential for. The
`credential.ref: deepseek_main` line points to a credential entry in
the daemon's vault — set it once via the web client (Settings →
Credentials) before the first turn.

## Files

- `app.yaml` — the entire app config (~30 useful lines)
- `README.md` — this file

That's it. No frontend code, no backend code. The agent does the
work; the modules carry the plumbing.
