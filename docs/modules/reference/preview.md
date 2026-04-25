---
id: preview
title: Preview Module
sidebar_label: preview
description: Per-session live canvas transport — state, resources, ReactFlow nodes, events. Streams over Socket.IO to the app's web UI.
---

# preview

The **preview** module is Digitorn's universal live-canvas transport layer. Agents
push state, named resources, and canvas nodes to a per-session stream that the
app's `web/` UI consumes via Socket.IO (namespace `/events`, room
`session:{session_id}`). This is what powers zero-code live previews for
digitorn-builder, React sandboxes, timelines, YAML panels, and any future
workflow editor or multi-agent orchestrator.

| Property | Value |
|----------|-------|
| **Module ID** | `preview` |
| **Version** | `1.0.0` |
| **Transport** | Socket.IO (namespace `/events`, room `session:{id}`) |
| **Actions exposed to LLM** | 0 — all 17 are `internal=True` |
| **Caller** | `workspace` module (Python-direct) |
| **Isolation** | Per-session (`PreviewSessionState`) |

---

## Architecture

```
┌───────────┐   Python calls     ┌─────────────┐   Socket.IO    ┌──────────┐
│   Agent   │ ───────────────▶  │  workspace  │ ────▶ preview  ────▶ web UI │
│           │   (WsWrite etc.)   │             │     (bus)     │  (React) │
└───────────┘                    └─────────────┘                └──────────┘
```

The agent never calls `preview.*` directly — every action is marked
`internal=True` and stripped from the tool schema sent to the LLM. Instead,
the **workspace** module (and any other shell-layer module) calls preview as
Python methods via its injected `self._preview` reference:

```python
await self._preview.set_resource(SetResourceParams(
    channel="files",
    id="src/App.tsx",
    payload={"content": "...", "language": "tsx"},
))
```

Per-session isolation is handled by `PreviewSessionStore`: each `session_id`
gets its own `PreviewSessionState` with independent `state`, `resources`,
`events` ring buffer, and monotonic `seq` counter. On reconnect the client
receives a full `preview:snapshot` replay, then resumes on live events.

See [CLAUDE.md](../../../CLAUDE.md) (section *Preview module — internal
Socket.IO transport layer*) for architecture context.

---

## Configuration

The preview module has **no user-facing config fields** — it's pure plumbing.
All behavior is driven by the calls made by upstream modules (workspace,
widget, custom shells).

```yaml
modules:
  preview: {}   # just enable it; no config needed
```
Two wired-in attributes are injected by the daemon bootstrap:

- `preview._event_bus` — the `SocketIOBus` used to publish events
- `preview._bus_app_id` — the app id used in the bus routing key

---

## Actions (17, all `internal=True`)

### State map (5)

| Action | Params | Purpose |
|--------|--------|---------|
| `set_state` | `key: str`, `value: Any` | Upsert one scalar into the state map |
| `patch_state` | `patch: dict` | Merge fields into the state map |
| `get_state` | — | Return full snapshot (state + resources) |
| `clear` | — | Wipe state, resources, and events |
| `emit` | `event_type: str`, `data: dict` | Push a free-form event to the stream |

### Named resources (6)

Generic channel primitive any shell can plug into. Channels are dicts keyed
by `id`, values are arbitrary JSON-serialisable payloads (e.g. `files`,
`slides`, `cells`, `nodes`, `edges`).

| Action | Params | Purpose |
|--------|--------|---------|
| `set_resource` | `channel: str`, `id: str`, `payload: dict` | Upsert a resource |
| `patch_resource` | `channel`, `id`, `patch: dict` | Merge fields; create if absent |
| `delete_resource` | `channel`, `id` | Remove one resource |
| `list_resources` | `channel` | Dump every id+payload in a channel |
| `bulk_set_resources` | `channel`, `items: dict[id, payload]`, `replace: bool=False` | Batch upsert (snapshot/import) |
| `clear_channel` | `channel` | Drop every resource in a channel |

### ReactFlow canvas (6)

Thin wrappers over `set_resource("nodes", ...)` and `set_resource("edges", ...)`.
Used by digitorn-builder and any ReactFlow-shaped canvas UI.

| Action | Params | Purpose |
|--------|--------|---------|
| `push_node` | `id?`, `type="default"`, `label=""`, `position={x,y}`, `data={}`, `status="idle"` | Add/replace a canvas node |
| `update_node` | `id`, `updates: dict` | Partial update; unknown keys merge into `data` |
| `highlight_node` | `id`, `status: idle\|running\|done\|error` | Shortcut to set node status |
| `remove_node` | `id` | Drop the node and any touching edges |
| `push_edge` | `id?`, `source`, `target`, `label=""`, `data={}` | Add/replace an edge |
| `remove_edge` | `id` | Drop one edge |

When `push_node.id` is omitted it's auto-derived by slugifying `label`
(fallback: `node-{N+1}`). Edge ids default to `"{source}->{target}"`.

---

## Socket.IO event types

Every mutation appends a `PreviewEvent` to the session's ring buffer with
an incrementing `seq`, then publishes on the bus as:

```json
{ "type": "preview:<event_type>", "data": { ...payload, "preview_seq": 42 } }
```

| Event type | Emitted by | Payload shape |
|------------|------------|---------------|
| `preview:state_changed` | `set_state` | `{key, value, preview_seq}` |
| `preview:state_patched` | `patch_state` | `{patch, preview_seq}` |
| `preview:cleared` | `clear` | `{preview_seq}` |
| `preview:resource_set` | `set_resource`, `push_node`, `push_edge` | `{channel, id, payload, preview_seq}` |
| `preview:resource_patched` | `patch_resource`, `update_node`, `highlight_node` | `{channel, id, patch, payload, preview_seq}` |
| `preview:resource_deleted` | `delete_resource`, `remove_node`, `remove_edge` | `{channel, id, preview_seq}` |
| `preview:resource_bulk_set` | `bulk_set_resources` | `{channel, items, replace, preview_seq}` |
| `preview:channel_cleared` | `clear_channel` | `{channel, preview_seq}` |
| `preview:snapshot` | Server → client on `join_session` | Full `PreviewSessionState.snapshot()` |

Clients use the monotonic `preview_seq` to reconcile after a reconnect: they
ask for the latest snapshot (published as `preview:snapshot` in the join
handshake), then drop any live events with `preview_seq <= snapshot.seq`.

---

## Session isolation & lifecycle

| Hook | Behavior |
|------|----------|
| `set_active_session(sid, uid)` | Called by the agent loop before each tool dispatch. Binds the next action to this session. |
| `_session()` | Resolves to `PreviewSessionState` via the store; creates one on demand. |
| `cleanup_session(sid)` | Drops all state for a session (called on session end). |
| `snapshot_for(sid, uid)` | Returns the replay payload used by the Socket.IO `join_session` handler. |

If no active session has been set (dev/tests without agent loop wiring),
a synthetic `_default_` session is used. In production this never happens —
the agent loop always calls `set_active_session` before dispatching.

---

## Integration notes

- **Agents don't see these tools.** All 17 actions are `internal=True` — the
  schema is never shipped to the LLM. Agents manipulate the preview indirectly
  through `workspace.*` (short names: WsWrite, WsRead, WsEdit, WsGlob, WsGrep,
  WsDelete).
- **Bootstrap wires the bus.** `bootstrap.py` sets `preview._event_bus` and
  `preview._bus_app_id` after the daemon SocketIOBus is instantiated. If either
  is missing, events are logged and dropped (warning: `preview_event_dropped`).
- **No SSE.** All streaming moved to Socket.IO; there are no fallback HTTP/SSE
  paths in this module.
- **No workbench.** The legacy workbench was removed; every live UI now uses
  this module.

---

## Related

- [`workspace`](./workspace.md) — the 6-action façade agents actually call
- [`widget`](./widget.md) — parallel transport for declarative Flutter widgets
- `CLAUDE.md` — section *Preview module — internal Socket.IO transport layer*
