# Socket.IO Protocol

Digitorn streams all runtime events via Socket.IO. This is the only streaming
transport - the legacy SSE endpoints have been removed.

> **See also**:
> - [API Integration -> Real-time](../../language/14-api-integration.md) - full REST + Socket.IO surface.
> - [preview module](../modules/preview.md) - `preview:*` events.
> - [widget module](../modules/widget.md) - `widget:*` events.

## Connection

- **Namespace**: `/events`
- **URL**: `ws://{host}:{port}/socket.io/?token={jwt}`
- **Transports**: `["websocket"]` only (HTTP polling is rejected by the daemon)

### Authentication

Token is passed in the URL query param. Browser WebSocket cannot send custom
headers, so query param is the only reliable method:

```javascript
const socket = io("http://127.0.0.1:8000/events?token=" + encodeURIComponent(token), {
  transports: ["websocket"],
  auth: { token },
  forceNew: true,
});
```

The token sources the server checks, in order:
1. `auth={'token': ...}` Socket.IO auth object
2. `Authorization: Bearer <t>` header (not available in browsers)
3. `?token=<t>` query string (the reliable path)
4. `digitorn_preview_token` cookie (set by HTTP middleware for preview routes)

### CORS

The daemon auto-adds its own origin (`http://{host}:{port}`) to the CORS
allowlist, so preview iframes served from the same host can connect without
custom configuration.

## Rooms

Events are routed to the most-specific room that matches:

1. `session:{session_id}` - one session's events
2. `app:{app_id}` - all events for an app
3. `user:{user_id}` - user-level inbox/notifications

Clients join a session after connect:

```javascript
socket.on("connect",.
 => {
  socket.emit("join_session", {
    app_id: "my-app",
    session_id: "abc-123",
    since: 0,  // replay events from seq >= since
  }, (ack) => {
    // { ok: true, room: "session:abc-123", latest_seq: 42 }
  });
});
```

## Event Envelope

Every event emitted on the `/events` namespace has this shape:

```json
{
  "type": "preview:resource_set",
  "seq": 42,
  "kind": "session",
  "app_id": "my-app",
  "session_id": "abc-123",
  "payload": { ... },
  "ts": "2026-04-16T22:00:00Z"
}
```

Fields:
- `type` - event type (see list below)
- `seq` - monotonic sequence number (for replay ordering)
- `kind` - routing kind: `session` / `error` / `approval` / `background_activation` / `status`
- `payload` - event-specific data
- `ts` - ISO 8601 timestamp

## Event Types

Defined in ### Agent session events

| Type | Payload |
|------|---------|
| `token` / `out_token` | `{content}` - streamed text chunk |
| `in_token` | `{content}` - incoming token (less common) |
| `token_usage` | `{input, output, total}` |
| `thinking` / `thinking_started` / `thinking_delta` | `{text}` - Claude reasoning |
| `tool_start` | `{id, name, params, label, detail}` |
| `tool_call` | `{id, name, params, success, error, result}` |
| `turn_complete` / `stream_done` | `{content, tool_calls_count, turns_used, error}` |
| `abort` | `{}` - turn interrupted |
| `result` | `{content, metadata}` - one_shot mode output |
| `memory_update` | `{key, value}` - memory mutation |
| `agent_event` | spawn/progress/result/cancel - sub-agent lifecycle |
| `hook` / `hook_notification` | `{hook_id, action_type, phase, details}` |
| `bg_task_update` | `{task_id, status}` |
| `terminal_output` | `{content}` - shell bg task output |
| `notification` | `{message, level}` |

### Preview events (workspace files, canvas state)

| Type | Payload |
|------|---------|
| `preview:snapshot` | `{state, resources, events, seq}` - full state replay |
| `preview:state_changed` | `{key, value, preview_seq}` |
| `preview:state_patched` | `{patch, preview_seq}` |
| `preview:cleared` | `{preview_seq}` |
| `preview:resource_set` | `{channel, id, payload, preview_seq}` - most common |
| `preview:resource_patched` | `{channel, id, payload, preview_seq}` |
| `preview:resource_deleted` | `{channel, id, preview_seq}` |
| `preview:resource_bulk_set` | `{channel, items, replace, preview_seq}` |
| `preview:channel_cleared` | `{channel, preview_seq}` |

### Widget events (declarative UI)

| Type | Payload |
|------|---------|
| `widget:render` | `{widget_id, type, props}` |
| `widget:update` | `{widget_id, props}` |
| `widget:close` | `{widget_id}` |
| `widget:error` | `{widget_id, message}` |
| `widget:state` | `{widget_id, state}` |
| `widget:cleared` | `{}` |
| `widget:snapshot` | full widget state |

### System / approval events

| Type | Kind | Payload |
|------|------|---------|
| `approval_request` | `approval` | `{request_id, tool, params}` |
| `credential_required` | `error` | `{provider, field, message}` |
| `credential_auth_required` | `error` | OAuth flow details |
| `error` | `error` | `{code, message, detail}` |
| `status` | `status` | `{phase, ...}` (requesting, generating, thinking, tool_use, rate_limited, waiting) |
| `notification_result` | - | approval result |

## Replay Semantics

When a client reconnects with `since: N`:

1. `EventBuffer` contains the last N recent events per user (in-memory, capped)
2. Server replays missed events in order (emits individual envelopes)
3. If `N > buffer_size`, the server emits a `preview:snapshot` to reconstruct
   the full state

Events are ALSO persisted per-turn in SQLite via
`save_turn_events`. The session-history endpoint aggregates
all turn event logs chronologically for full replay.

## Client Handlers

```javascript
socket.on("event", (envelope) => {
  const { type, seq, payload } = envelope;

  if (type.startsWith("preview:")) {
    // preview events - dispatch to preview reducer
    handlePreview(type, payload);
  } else if (type.startsWith("widget:")) {
    // widget events
    handleWidget(type, payload);
  } else {
    // session events (tokens, tool_call, turn_complete, etc.)
    handleSession(type, payload);
  }
});
```

## Emit events from client

Clients can emit these events to the server:

| Event | Data | Purpose |
|-------|------|---------|
| `join_session` | `{app_id, session_id, since?}` | Subscribe to a session room |
| `join_app` | `{app_id}` | Subscribe to app-level events |
| `leave_session` | `{session_id}` | Unsubscribe |
| `leave_app` | `{app_id}` | Unsubscribe |
| `send_message` | `{app_id, session_id, message, images?, workspace?}` | Run an agent turn (alternative to POST /messages) |
| `replay` | `{user_id, since}` | Replay events from a sequence |

## Error Codes

Connection rejections:
- `403` - auth failed (invalid/missing token)
- `429` - rate-limited (too many rejected connections from this IP)
- `400` - POST polling (intentionally rejected - use WebSocket transport)
