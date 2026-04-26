# preview — Integration Guide

`preview` is the **Socket.IO transport layer** for live-preview apps.
It exposes a tiny primitive surface that higher-level modules (notably
`workspace`) use to stream state + resource updates to connected
clients in real time. All 17 actions are marked ``internal=True`` —
they are NOT exposed to the LLM.

## Three primitive ops — everything else builds on these

| Primitive | Shape | Use |
|---|---|---|
| **state** | key → scalar | `set_state(key, value)`, `patch_state({...})`, `get_state()` |
| **resources** | channel → id → payload | `set_resource(channel, id, payload)`, `patch_resource(...)`, `delete_resource(...)`, `bulk_set_resources(...)`, `clear_channel(...)` |
| **events** | ephemeral | `emit(event_type, data)` |

Per-session scoping is automatic — the daemon calls
`set_active_session(sid)` at the start of each agent turn; all
`set_*` / `patch_*` / `emit` ops route to that sid's room on the
`/events` Socket.IO namespace.

## Why modules call this, not the LLM

```
                                   LLM (agent turn)
                                       │
                                       ▼
                              WsWrite / WsEdit ...        ← LLM-visible
                                       │
                                       ▼
                         workspace module (Python)
                                       │
                                       ▼
           preview.set_resource("files", path, payload)    ← internal
                                       │
                                       ▼
               Socket.IO emit on room "session:sid"
                                       │
                                       ▼
                   React / Flutter client renders the diff
```

The LLM never "renders" anything — it just writes files through
`workspace.*`, and `workspace` fans the mutation out through `preview`.

## Constraints

No constraints — this module has no safety surface. The protection is
that its actions are `internal=True` and unreachable from the agent.

## Isolation

`preview` is `shared` across an app (one instance per app, all
sessions share it) but every write is keyed by the active session id,
so two parallel sessions stream to distinct Socket.IO rooms.

## Channels

`resources` are namespaced by **channel**:

| Channel | Typical content |
|---|---|
| `files` | Live virtual file system (workspace module) |
| `nodes` / `edges` | Graph canvas data (builder render_mode) |
| `slides` | Slide deck (LaTeX / slides render_mode) |
| *custom* | Any key the app wants to publish |

## Related

- `modules/workspace/module.py` — top consumer of `preview`
- `docs/PREVIEW.md` — high-level architecture doc
- `docs/PREVIEW_ARCHITECTURE.md` — deep-dive on SSE / SDK
