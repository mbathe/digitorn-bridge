---
id: flutter_socketio_integration
title: Flutter Socket.IO Integration
sidebar_label: Flutter / Socket.IO
---

# Flutter Socket.IO Integration

Canonical guide for building a Flutter (or any Socket.IO v4
client) against the Digitorn daemon. **One Socket.IO
connection** handles every real-time surface - agent
streaming tokens, tool calls, thinking blocks, errors,
approvals, sub-agents, preview state, widgets, credentials
prompts, background activations.

> **No SSE.** All session events flow through Socket.IO
> rooms on the `/events` namespace. The legacy SSE endpoints
> have been removed. **HTTP REST** is kept for CRUD only -
> create session, list sessions, list apps, send message via
> POST when you don't have a socket.

For the comprehensive REST + Socket.IO surface see
[API Integration](app-language/14-api-integration.md).

## 1 · Connection

### Transport

- **Library** - `socket_io_client` (Dart) or any Socket.IO
  v4-compatible client.
- **URL** - `http(s)://<daemon_host>:<port>`
- **Namespace** - `/events`
- **Transport** - WebSocket only (`transports: ['websocket']`)

### Authentication

The server accepts JWT tokens from four sources, checked in
order (`socketio_bus.py:295-327`):

1. `auth: {token: '<jwt>'}` - **recommended** (Socket.IO
   standard).
2. `Authorization: Bearer <jwt>` header.
3. `?token=<jwt>` query string (browser fallback).
4. `digitorn_preview_token` cookie (preview-iframe scenario;
   set by the daemon's HTTP middleware on `/preview/*` routes).

```dart
final socket = io('http://localhost:8000/events', <String, dynamic>{
  'transports': ['websocket'],
  'auth': {'token': jwtAccessToken},
  'forceNew': true,
});
```

When auth is disabled on the daemon (dev mode), no token is
needed - the server assigns `user_id: "local"`.

### Handshake

On successful connect the server immediately emits a single
`event` with `type: "connected"`:

```json
{
  "type": "connected",
  "seq": 42,
  "kind": "system",
  "app_id": null,
  "session_id": null,
  "payload": {},
  "ts": "2026-04-15T10:23:11.000000+00:00",
  "capabilities": ["full_events"],
  "latest_seq": 42,
  "user_id": "alice"
}
```

- **`latest_seq`** - the highest seq the server has for this
  user. Store it locally; on reconnect, pass it as `since`
  to catch up.
- **`user_id`** - the authenticated user (or `"local"` in
  dev mode).
- The client is auto-joined to room `user:{user_id}`.

### Connection rejected

Missing / expired / invalid JWT → connection rejected. The
client library raises a connection error. **Rejected**
connections are rate-limited per IP via
`websocket.rate_limit_max_connections` (default 5) within
`websocket.rate_limit_window` seconds (default 10) - only
failed attempts count, successful connects don't.

## 2 · Rooms

Three room tiers, from most to least specific:

| Room | Format | Auto? | Use |
|------|--------|:-----:|-----|
| **user** | `user:{user_id}` | yes (on connect) | Global notifications, approval requests, inbox. |
| **app** | `app:{app_id}` | no (`join_app`) | All events for any session of that app. |
| **session** | `session:{session_id}` | no (`join_session`) | Events for one specific session. |

### Routing rule

Each event is emitted to the **most specific** room that
matches:

- Session events → `session:{id}`.
- App-level events (no session_id) → `app:{id}`.
- User-level events → `user:{uid}`.

**`approval_request` is special** - it fans out to BOTH
`session:{id}` AND `user:{uid}` so the global inbox badge
sees it even when the user isn't viewing that session.

## 3 · Client commands (emit → ack)

All commands emit on the `/events` namespace and return an
ack dict.

### `join_session`

```dart
final ack = await socket.emitWithAck('join_session', {
  'app_id': 'my-app',
  'session_id': 'sess-1234',
  'since': lastKnownSeq,    // optional - replay missed events
});
// ack = {ok: true, room: 'session:sess-1234', latest_seq: 57}
// ack = {ok: false, error: 'session not found or access denied'}
```

- **Ownership check** - the server verifies the session
  belongs to the authenticated user. Cross-user join is
  rejected.
- **Replay** - when `since > 0`, the server replays all
  buffered events with `seq > since` for that session
  directly to this client (not broadcast).

### `leave_session`

```dart
final ack = await socket.emitWithAck('leave_session', {
  'session_id': 'sess-1234',
});
// ack = {ok: true}
```

### `join_app` / `leave_app`

```dart
final ack = await socket.emitWithAck('join_app', {
  'app_id': 'my-app',
  'since': lastKnownSeq,    // optional
});
// ack = {ok: true, room: 'app:my-app', latest_seq: 57}

await socket.emitWithAck('leave_app', { 'app_id': 'my-app' });
```

### `send_message`

Replaces `POST /sessions/{sid}/messages` for real-time chat
(stays available as REST fallback).

```dart
final ack = await socket.emitWithAck('send_message', {
  'app_id': 'my-app',
  'session_id': 'sess-1234',
  'message': 'Hello, what can you do?',
  'workspace': '/home/user/project',     // optional
  'images': [                             // optional, max 10
    {'data': base64String, 'mime': 'image/png', 'name': 'screenshot.png'},
  ],
});
// ack = {ok: true, accepted: true}
// ack = {ok: false, error: 'app_id, session_id and message required'}
```

Returns immediately (fire-and-forget). The agent turn runs
in the background; all events flow through the session room
you already joined. Errors during the turn are emitted as
`type: "error"` events on that same room.

### `replay`

On-demand replay (useful after a network blip without full
reconnect):

```dart
final ack = await socket.emitWithAck('replay', {
  'since': lastKnownSeq,
  'session_id': 'sess-1234',    // optional filter
  'app_id': 'my-app',           // optional filter
});
// ack = {ok: true, replayed: 6, latest_seq: 63}
```

Replayed events are emitted to this client only.

### `latest_seq`

Quick query (no replay):

```dart
final ack = await socket.emitWithAck('latest_seq', null);
// ack = {ok: true, latest_seq: 63}
```

### `ping` (default namespace)

Health check on `/`, not `/events`:

```dart
final ack = await defaultSocket.emitWithAck('ping', null);
// ack = {pong: true}
```

## 4 · Event envelope (universal shape)

All events arrive on a single listener:

```dart
socket.on('event', (data) {
  final envelope = data as Map<String, dynamic>;
  // handle envelope
});
```

Every envelope:

```json
{
  "type": "token",
  "seq": 42,
  "kind": "session",
  "app_id": "my-app",
  "session_id": "sess-1234",
  "payload": { ... },
  "ts": "2026-04-15T10:23:11.123456+00:00"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Event type (catalogue below). |
| `seq` | int | Monotonic per user. **Never goes backward.** Use for replay gap detection. |
| `kind` | string | Logical category - `session` / `error` / `approval` / `background_activation` / `status` / `system`. |
| `app_id` | string \| null | Which app emitted (null for user-level events). |
| `session_id` | string \| null | Which session (null for app- or user-level events). |
| `payload` | map | Event-specific data (see catalogue below). |
| `ts` | string | ISO 8601 UTC timestamp. |

## 5 · Event catalogue

### Streaming text + thinking

| Event | Payload | Behaviour |
|-------|---------|-----------|
| `token` | `{delta}` | Append to streaming buffer. |
| `stream_done` | `{}` | LLM finished producing text (before tools execute). |
| `thinking_started` | `{}` | LLM entered an extended-thinking block. |
| `thinking_delta` | `{delta}` | Incremental thinking text - render in a collapsible block. |
| `thinking` | `{text}` | Complete thinking text (some turns emit only this - fallback if no deltas). |
| `assistant_delta` / `assistant_message` | `{text}` | Some providers stream as `assistant_delta` instead of `token`. |

### Tool execution

| Event | Payload | Behaviour |
|-------|---------|-----------|
| `tool_start` | `{id, name, params, label, detail, display}` | Tool about to execute. `display: {icon, verb, color}` are UI hints. |
| `tool_call` | `{id, name, params, success, error, result, label, detail, display, diff?, previous_content?, new_content?, image_data?, image_mime?}` | Tool finished. `result` is tool-specific; `diff` / content fields populated for Write / Edit; `image_data` / `image_mime` for tools that return images (e.g. Read on a PNG). |

### Derived events (alongside tool_call)

| Event | Payload | Trigger |
|-------|---------|---------|
| `memory_update` | `{action, name, result}` | A memory tool ran (`memory.set_goal`, `Remember`, `TaskCreate`, `TaskUpdate`). |
| `terminal_output` | `{stdout, stderr}` | A shell command produced output (`Bash`). Stdout truncated at 2000 chars, stderr at 500. |
| `agent_event` | `{action, name, result}` | A sub-agent action (`spawn_agent`, `agent_progress`, `agent_result`, `agent_cancel`). |

### Turn completion

| Event | Payload | Notes |
|-------|---------|-------|
| `result` | `{content, session_id, tool_calls_count, turns_used, truncated, error, usage, turn_number, context}` | The agent turn is finished. Contains usage / cost data. **Most important event** - signals the turn is complete. |
| `error` | `_classify_error()` payload (see [Error classification](app-language/14-api-integration.md#error-classification)) | Turn-level error. |

`usage` shape:

```json
{
  "input_tokens": 1200,
  "output_tokens": 350,
  "total_input_tokens": 5400,
  "total_output_tokens": 1200,
  "total_tokens": 6600,
  "cost_usd": 0.0234
}
```

### Approvals

| Event | Payload | Behaviour |
|-------|---------|-----------|
| `approval_request` | `{request_id, tool, params, timeout, reason}` | Tool needs explicit approval. Display dialog, then `POST /api/apps/{app_id}/approve` with `{request_id, approved, message}`. |
| `approval_resolved` | `{request_id, approved, message?}` | Resolution echoed (you've already POST-ed). |

> **Cross-room fanout** - `approval_request` arrives on
> BOTH the session room AND the user room so global inbox
> badges work.

### Credentials prompts

| Event | Payload | Trigger |
|-------|---------|---------|
| `credential_required` | `{provider, scope, fields}` | Tool needs a credential the user hasn't filled yet. Show the credential picker. |
| `credential_auth_required` | `{provider, auth_url}` | OAuth flow not yet completed. Open `auth_url` in a webview. |

### Preview (canvas)

`preview:*` events flow through the same socket. See
[preview module](modules/reference/preview.md) for the full
reference.

| Event | Payload |
|-------|---------|
| `preview:state_changed` | `{key, value, preview_seq}`. |
| `preview:state_patched` | `{patch, preview_seq}`. |
| `preview:resource_set` | `{channel, id, payload, preview_seq}`. |
| `preview:resource_patched` | `{channel, id, patch, payload, preview_seq}`. |
| `preview:resource_deleted` | `{channel, id, preview_seq}`. |
| `preview:resource_bulk_set` | `{channel, items, replace, preview_seq}`. |
| `preview:channel_cleared` | `{channel, preview_seq}`. |
| `preview:cleared` | `{preview_seq}`. |
| `preview:snapshot` | Full state snapshot - sent on `join_session` for catch-up. |

### Widgets

`widget:*` events. See
[widget module](modules/reference/widget.md).

| Event | Payload |
|-------|---------|
| `widget:render` | `MountedWidget.to_dict()`. |
| `widget:update` | `{widget_id, patch}`. |
| `widget:close` | `{widget_id, was_mounted}`. |
| `widget:error` | `{widget_id, binding, message}`. |
| `widget:state` | `{state}` (full snapshot post-merge). |
| `widget:cleared` | `{}`. |
| `widget:snapshot` | Full state snapshot on `join_session`. |

### Background activations

| Event | Payload | Trigger |
|-------|---------|---------|
| `background_activation_started` | `{activation_id, trigger_id}` | Trigger fired, agent turn started. |
| `background_activation_finished` | `{activation_id, status, result, error?}` | Activation finished. |

### Hooks + behaviour

| Event | Payload | Trigger |
|-------|---------|---------|
| `hook` | `{event, action, ...}` | A YAML hook fired. |
| `behavior` | `{rule_id, action, message}` | The behaviour engine injected a `[BEHAVIOR ...]` message. |

### Abort + system

| Event | Payload | Behaviour |
|-------|---------|-----------|
| `abort` | `{reason}` | Session abort triggered. Show "Interrupted by user" banner. |
| `notification` | `{title, body, level, ...}` | Generic notification (background tasks, system). |
| `connected` | `{capabilities, latest_seq, user_id}` | Initial handshake. |
| `pong` | `{...}` | Reply to `ping`. |

## 6 · Replay + sequence handling

The server buffers a **per-user ring** of recent events with
monotonic `seq` numbers. On reconnect:

1. Read your last seen `latest_seq` from local storage.
2. Connect; the handshake gives you the new `latest_seq`.
3. If `new_latest_seq > old_latest_seq`, call `replay`
   with `since: old_latest_seq` - the server replays missed
   events to this client.
4. Persist the new `latest_seq` after each event.

Use `seq` strictly for gap detection - never for ordering
within a turn (use `ts` for chronological display).

## 7 · Recommended Flutter pattern

```dart
class DigitornClient {
  final Socket socket;
  int latestSeq = 0;

  DigitornClient(String baseUrl, String jwt)
    : socket = io('$baseUrl/events', {
        'transports': ['websocket'],
        'auth': {'token': jwt},
        'forceNew': true,
      });

  Future<void> connect() async {
    socket.onConnect((_) async {
      // 1. Wait for handshake
      // 2. Replay missed events
      if (latestSeq > 0) {
        await socket.emitWithAck('replay', {'since': latestSeq});
      }
    });

    socket.on('event', _handleEvent);
  }

  void _handleEvent(dynamic data) {
    final env = data as Map<String, dynamic>;
    latestSeq = env['seq'] as int;     // always advance

    switch (env['type']) {
      case 'connected':
        // handshake - store user_id, capabilities
        break;
      case 'token':
        _appendStreaming(env['payload']['delta']);
        break;
      case 'tool_call':
        _renderToolCall(env['payload']);
        break;
      case 'result':
        _finishTurn(env['payload']);
        break;
      case 'error':
        _showErrorBanner(env['payload']);
        break;
      case 'approval_request':
        _askApproval(env['payload']);
        break;
      case 'credential_required':
        _showCredentialPicker(env['payload']);
        break;
      // ... preview:*, widget:*, etc.
    }
  }

  Future<void> joinSession(String appId, String sessionId) async {
    final ack = await socket.emitWithAck('join_session', {
      'app_id': appId,
      'session_id': sessionId,
      'since': latestSeq,
    });
    if (!ack['ok']) throw Exception(ack['error']);
  }

  Future<void> sendMessage(String appId, String sessionId, String message) async {
    final ack = await socket.emitWithAck('send_message', {
      'app_id': appId,
      'session_id': sessionId,
      'message': message,
    });
    if (!ack['ok']) throw Exception(ack['error']);
    // The reply arrives via the session room as token / tool_call / result events.
  }

  void disconnect() => socket.disconnect();
}
```

## 8 · REST surface (CRUD)

Use REST for everything **not** real-time:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/apps/{app_id}/sessions` | Create a session. |
| `GET` | `/api/apps/{app_id}/sessions` | List sessions. |
| `GET` | `/api/apps/{app_id}/sessions/{sid}` | Session metadata. |
| `GET` | `/api/apps/{app_id}/sessions/{sid}/history` | Full message history. |
| `DELETE` | `/api/apps/{app_id}/sessions/{sid}` | Delete a session. |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/abort` | Cancel an in-flight turn. |
| `POST` | `/api/apps/{app_id}/approve` | Resolve an approval request. |
| `GET` | `/api/apps` | List deployed apps. |
| `POST` | `/api/apps/install` | Install / deploy an app. |
| ... | ... | The full REST surface lives in [API Integration](app-language/14-api-integration.md). |

## 9 · Deprecation notes

- **SSE removed** - every legacy SSE endpoint
  (`/api/apps/{id}/sessions/{sid}/events`,
  `/api/apps/{id}/widget-events`, ...) is gone. Use Socket.IO.
- **`POST /sessions/{sid}/messages`** still works as a REST
  fallback for clients that can't use Socket.IO. Real-time
  push still flows through the session room - connect a
  socket and `join_session` to receive replies.

## Cross-references

- Comprehensive REST + Socket.IO reference (every
  endpoint cited): [API Integration](app-language/14-api-integration.md)
- Auth flow (where the JWT comes from):
  [Auth](app-language/22-auth.md)
- Approval gate semantics:
  [Security](app-language/11-security.md)
- Preview module + events:
  [preview reference](modules/reference/preview.md)
- Widget module + events:
  [widget reference](modules/reference/widget.md)
- Multi-user routing of background activations:
  [Background Sessions](app-language/38-background-sessions.md)
