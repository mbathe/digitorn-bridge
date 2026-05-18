---
id: web_preview
title: web_preview module
---

# web_preview

Session-scoped iframe preview attachments. The agent points the
client's Preview tab at a running dev server it spawned via Bash.
The daemon stores the `(session_id, name) -> port/host` mapping
and emits a Socket.IO `web_preview:attached` event carrying the
direct-connect URL the client should load.

The daemon does **NOT** proxy HTTP, **NOT** serve static files,
**NOT** spawn processes - it is purely a registry. The browser
hits the dev server directly.

## When to use it

- The agent is building or modifying a web app and the user wants
  to see live HMR ("Lovable-style").
- The app ships a pre-built `web/dist/` and the SDK auto-attaches
  it at session create.
- The agent has spawned several services in parallel (frontend +
  backend + admin) and wants to expose each one via a named
  attachment.

For static-built apps (`npm run build` -> `dist/`), the agent runs
`python -m http.server` on a port via Bash, then attaches via
this module. Same code path as a real dev server.

## Module config

```yaml
tools:
  modules:
    web_preview: {}              # no required config
```

The `WebPreviewConfig` model is intentionally empty for v1. It
accepts arbitrary keys (`extra: allow`) for forward-compat with
future deployment-targeting options.

## Actions

| Action | FQN | Short name | Purpose |
|--------|-----|-----------|---------|
| Attach | `web_preview.proxy` | `PreviewProxy` | Register an iframe target on a port the agent has spawned. |
| Publish | `web_preview.publish` | `PreviewPublish` | Build the project once and serve the static output same-origin under `/api/apps/{id}/sessions/{sid}/published/`. Right for cloud / multi-tenant deploys. |
| Detach | `web_preview.detach` | (no short alias) | Drop a registered attachment. |

### `PreviewProxy(port, [name], [path], [bash_task_id])`

Visible params:

- `port` (int, 1-65535, **required**): TCP port the dev server is
  listening on.
- `name` (str, default `"default"`): Logical name. Use when
  multiple previews coexist in one session (e.g. `"frontend"`,
  `"backend"`, `"admin"`).
- `path` (str, default `""`): URL path appended after `host:port`,
  e.g. `"/landing.html"`. Always start with `/` when set.
- `bash_task_id` (str | null, default `null`): If you spawned the
  dev server via `Bash(run_in_background=true)`, pass the returned
  `task_id` so the daemon can auto-kill the process when the
  attachment is reaped due to inactivity.

Hidden params (advanced, schema not exposed to the LLM):

- `host` (default `"127.0.0.1"`).
- `health_check` (bool, default `true`): HEAD probe before
  registering.
- `wait_seconds` (int 0-120, default 0): override the 15-second
  bind-wait budget for slow frameworks.

Effect: emits `web_preview:attached` on the session's Socket.IO
room with `{name, url, host, port, path}`. The client connects the
iframe to that URL.

### `PreviewPublish(install?, build_script?, output_dir?, name?)`

Build the project once (via `npm install` + `npm run build`,
or the script you set in `build_script`) and copy the static
output (`output_dir`, default `dist/`) to
`~/.digitorn/published/<app_id>/<session_id>/`. Register a
`published` attachment and emit `web_preview:attached` so the
iframe reloads at the new same-origin URL.

Use `PreviewPublish` when the daemon is cloud-hosted (no port
to expose), for shareable demo URLs, or any deploy where a live
dev server per session is too expensive. Use `PreviewProxy`
instead when you have a live Vite/HMR loop on a local machine.

### `web_preview.detach(name="default")`

Drops the attachment registered under `name` for the current
session. The matching bash task (if any) is killed (best-effort).

## Limits

Two ceilings are enforced at attach time. Hitting either returns
a clear error so the agent can detach an existing entry or pick
a different strategy:

- 5 attachments per session
- 20 attachments per user

These are roomy for legitimate use (frontend + backend + docs +
admin = 4) and tight enough to catch runaway loops.

## Lifecycle

Attachments are session-scoped: two different sessions of the
same app see two independent previews. Attachments are dropped
when:

- The agent calls `detach`.
- The session is destroyed (`session_end`).
- The reaper loop fires - 30 minutes with no HTTP traffic =
  considered abandoned, dropped, the matching bash task killed.

The reaper runs every 5 minutes.

## Security

This module exposes a registry, not a proxy. The browser
connects directly to whatever host:port the agent registered.
Implications:

- The dev server **must** be reachable from the user's network.
  `host: "127.0.0.1"` (the default) means the user's browser
  reaches it on their own machine - which is fine for local
  daemon use, but not for production-deployed daemons exposed
  to remote users.
- For production deployments behind a reverse proxy, configure
  the proxy to forward the relevant ports OR ship a pre-built
  bundle and use the static-bundle attachment path.
- The agent SHOULD spawn dev servers under a sandboxed user and
  on a port within an allowed range. The OS sandbox
  ([sandbox](../../language/35-sandbox.md)) gates this when set.

## Example

```yaml
app:
  app_id: lovable-clone
  name: "Lovable Clone"

runtime:
  mode: conversation
  workdir: "{{env.PWD}}/sandbox"

agents:
  - id: builder
    role: assistant
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      backend: anthropic
      config:
        api_key: "{{env.ANTHROPIC_API_KEY}}"
    system_prompt: |
      You build small React apps for the user.
      Use Bash to spawn the dev server in background, then
      PreviewProxy to attach the iframe.

tools:
  modules:
    filesystem: {}
    shell: {}
    web_preview: {}
  capabilities:
    default_policy: auto
```
