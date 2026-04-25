# Digitorn — Socket.IO Integration Spec for Flutter Client

> **Target:** Daemon v2 (Socket.IO migration complete). **ALL session events**
> now flow through Socket.IO rooms on the `/events` namespace:
> - Agent events (tokens, tool calls, thinking, errors, approvals, result)
> - Preview events (`preview:*` — canvas nodes, edges, files, state)
> - Widget events (`widget:*` — render, update, close, state)
> - Credential events (`credential_required`, `credential_auth_required`)
>
> **Zero SSE connections needed.** One Socket.IO connection handles everything.
>
> The old SSE endpoints still exist for backward compat but are **deprecated**.
> Flutter clients should NOT open them.
>
> **HTTP REST** is kept for CRUD only (create session, list sessions, list apps,
> send message via POST, etc.).

---

## 1. Connection

### Transport
- **Library:** `socket_io_client` (Dart) or any Socket.IO v4-compatible client.
- **URL:** `http(s)://<daemon_host>:<port>`
- **Namespace:** `/events`
- **Transport:** WebSocket only (`transports: ['websocket']`)

### Authentication
The server accepts JWT tokens from 3 sources (checked in order):
1. `auth: {'token': '<jwt>'}` — **recommended** (Socket.IO standard)
2. `Authorization: Bearer <jwt>` header
3. `?token=<jwt>` query string (browser fallback)

```dart
final socket = io('http://localhost:8765/events', <String, dynamic>{
  'transports': ['websocket'],
  'auth': {'token': jwtAccessToken},
  'forceNew': true,
});
```

If auth is disabled on the daemon (dev mode), no token is needed — the server
assigns `user_id: "local"` automatically.

### Handshake
On successful connect, the server immediately emits a single `event` with
`type: "connected"`:

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

- **`latest_seq`**: the highest seq number the server has for this user.
  Store it locally. On reconnect, pass it as `since` to catch up.
- **`user_id`**: the authenticated user (or `"local"` in dev mode).
- The client is automatically joined to room `user:{user_id}`.

### Connection rejected
If the JWT is missing, expired, or invalid, the server rejects the
connection. The client library raises a connection error. Rate limiting
applies to rejected connection attempts (30 per 10s window per IP).

---

## 2. Rooms & Event Routing

Three room tiers, from most to least specific:

| Room | Format | Auto? | Use |
|------|--------|-------|-----|
| **user** | `user:{user_id}` | Yes (on connect) | Global notifications, approval requests, inbox |
| **app** | `app:{app_id}` | No (`join_app`) | All events for any session of that app |
| **session** | `session:{session_id}` | No (`join_session`) | Events for one specific session |

**Routing rule (server-side):** each event is emitted to the **most specific
room** that matches. Session events go to `session:{id}`. App-level events
(no session_id) go to `app:{id}`. User-level events go to `user:{uid}`.

**`approval_request` is special:** it fans out to BOTH `session:{id}` AND
`user:{uid}` so the global inbox badge sees it even if the user is not
viewing that session.

---

## 3. Client Commands (emit → ack)

All commands are emitted on namespace `/events` and return an ack dict.

### `join_session`
```dart
final ack = await socket.emitWithAck('join_session', {
  'app_id': 'my-app',
  'session_id': 'sess-1234',
  'since': lastKnownSeq,  // optional: replay missed events
});
// ack = {"ok": true, "room": "session:sess-1234", "latest_seq": 57}
// ack = {"ok": false, "error": "session not found or access denied"}
```
- **Ownership check:** the server verifies the session belongs to the
  authenticated user. Cross-user join is rejected.
- **Replay:** if `since > 0`, the server replays all buffered events with
  `seq > since` for that session directly to this client (not broadcast).

### `leave_session`
```dart
final ack = await socket.emitWithAck('leave_session', {
  'session_id': 'sess-1234',
});
// ack = {"ok": true}
```

### `join_app`
```dart
final ack = await socket.emitWithAck('join_app', {
  'app_id': 'my-app',
  'since': lastKnownSeq,  // optional
});
// ack = {"ok": true, "room": "app:my-app", "latest_seq": 57}
```

### `leave_app`
```dart
final ack = await socket.emitWithAck('leave_app', {
  'app_id': 'my-app',
});
// ack = {"ok": true}
```

### `send_message`
**This replaces the old `POST /sessions/{sid}/messages` for real-time chat.**
```dart
final ack = await socket.emitWithAck('send_message', {
  'app_id': 'my-app',
  'session_id': 'sess-1234',
  'message': 'Hello, what can you do?',
  'workspace': '/home/user/project',  // optional
  'images': [                          // optional, max 10
    {'data': base64String, 'mime': 'image/png', 'name': 'screenshot.png'},
  ],
});
// ack = {"ok": true, "accepted": true}
// ack = {"ok": false, "error": "app_id, session_id and message required"}
```
- Returns **immediately** (fire-and-forget). The agent turn runs in the
  background. All events (tokens, tool calls, result) flow through the
  session room you already joined.
- Errors during the turn are emitted as `type: "error"` events on the
  same session room.

### `replay`
On-demand replay (useful after a network blip without full reconnect):
```dart
final ack = await socket.emitWithAck('replay', {
  'since': lastKnownSeq,
  'session_id': 'sess-1234',  // optional filter
  'app_id': 'my-app',         // optional filter
});
// ack = {"ok": true, "replayed": 6, "latest_seq": 63}
```
Replayed events are emitted to this client only (not broadcast).

### `latest_seq`
Quick query (no replay):
```dart
final ack = await socket.emitWithAck('latest_seq', null);
// ack = {"ok": true, "latest_seq": 63}
```

### `ping` (default namespace)
Health check (on `/` namespace, not `/events`):
```dart
// Connect to default namespace for ping
final ack = await defaultSocket.emitWithAck('ping', null);
// ack = {"pong": true}
```

---

## 4. Event Envelope (universal shape)

**All events** arrive as a single listener:
```dart
socket.on('event', (data) {
  final envelope = data as Map<String, dynamic>;
  // handle envelope
});
```

Every envelope has this exact shape:

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
| `type` | `string` | Event type (see table below) |
| `seq` | `int` | Monotonically increasing per user. **Never goes backward.** Use for replay gap detection. |
| `kind` | `string` | Logical category: `"session"`, `"error"`, `"approval"`, `"background_activation"`, `"status"`, `"system"` |
| `app_id` | `string?` | Which app emitted this (null for user-level events) |
| `session_id` | `string?` | Which session (null for app-level or user-level events) |
| `payload` | `Map` | Event-specific data (see §5) |
| `ts` | `string` | ISO 8601 UTC timestamp |

---

## 5. All Event Types & Payload Shapes

### 5.1 Streaming tokens

#### `token`
Emitted for each text fragment as the LLM generates it.
```json
{
  "type": "token",
  "kind": "session",
  "payload": {
    "delta": "Hello"
  }
}
```
Append `payload.delta` to the streaming text buffer.

#### `stream_done`
Signals the LLM finished producing text (before tool calls are executed).
```json
{
  "type": "stream_done",
  "kind": "session",
  "payload": {}
}
```

### 5.2 Thinking (extended thinking / chain-of-thought)

#### `thinking_started`
The LLM entered a thinking block.
```json
{
  "type": "thinking_started",
  "kind": "session",
  "payload": {}
}
```

#### `thinking_delta`
Incremental thinking text (stream it in a collapsible block).
```json
{
  "type": "thinking_delta",
  "kind": "session",
  "payload": {
    "delta": "Let me analyze the code..."
  }
}
```

#### `thinking`
Complete thinking text (emitted after the block closes). Some turns emit
only this (no deltas). Use as fallback if you didn't receive deltas.
```json
{
  "type": "thinking",
  "kind": "session",
  "payload": {
    "text": "I need to read the file first, then check..."
  }
}
```

### 5.3 Tool execution

#### `tool_start`
A tool is about to be executed.
```json
{
  "type": "tool_start",
  "kind": "session",
  "payload": {
    "id": "call_abc123",
    "name": "filesystem.read",
    "params": {"file_path": "/tmp/test.txt"},
    "label": "Read",
    "detail": "/tmp/test.txt",
    "display": {"icon": "📄", "verb": "Reading", "color": "#4A9"}
  }
}
```

#### `tool_call`
A tool finished executing (success or failure).
```json
{
  "type": "tool_call",
  "kind": "session",
  "payload": {
    "id": "call_abc123",
    "name": "filesystem.read",
    "params": {"file_path": "/tmp/test.txt"},
    "success": true,
    "error": "",
    "label": "Read",
    "detail": "/tmp/test.txt",
    "display": {"icon": "📄", "verb": "Reading", "color": "#4A9"},
    "result": {
      "content": "file contents here...",
      "size": 1234,
      "lines": 42
    },
    "diff": "--- a/file.txt\n+++ b/file.txt\n...",
    "previous_content": "old content",
    "new_content": "new content",
    "image_data": "<base64>",
    "image_mime": "image/png"
  }
}
```

**Important fields:**
- `success`: `bool` — always present.
- `error`: `string` — always present (empty if no error).
- `result`: `Map` — tool-specific data, always present.
- `diff`: `string?` — unified diff for Write/Edit tools (max 4000 chars).
- `previous_content` / `new_content`: `string?` — for frontend diff view.
- `image_data` / `image_mime`: for tools that produce images (e.g. Read on an image file).
- `label`: human-readable short name (e.g. "Read", "Edit", "Bash").
- `detail`: human-readable detail (e.g. file path, command).
- `display`: `{icon, verb, color}` — UI hints for rendering.

### 5.4 Derived events (emitted alongside tool_call)

#### `memory_update`
A memory tool ran (SetGoal, Remember, TodoAdd, TodoUpdate, etc.).
```json
{
  "type": "memory_update",
  "kind": "session",
  "payload": {
    "action": "set_goal",
    "name": "memory.set_goal",
    "result": {"goal": "Build the login page"}
  }
}
```

#### `terminal_output`
A shell command produced output (Bash, BashBackground).
```json
{
  "type": "terminal_output",
  "kind": "session",
  "payload": {
    "stdout": "total 42\ndrwxr-xr-x ...",
    "stderr": ""
  }
}
```
Truncated: stdout max 2000 chars, stderr max 500 chars.

#### `agent_event`
A sub-agent action (Agent, AgentWait, AgentResult, etc.).
```json
{
  "type": "agent_event",
  "kind": "session",
  "payload": {
    "action": "spawn_agent",
    "name": "agent_spawn.spawn_agent",
    "result": {"agent_id": "sub-1", "status": "running"}
  }
}
```

### 5.5 Turn completion

#### `result`
The agent turn is finished. This is the **most important event** — it
signals the turn is complete and contains usage/cost data.
```json
{
  "type": "result",
  "kind": "session",
  "payload": {
    "content": "Here's what I found...",
    "session_id": "sess-1234",
    "tool_calls_count": 3,
    "turns_used": 1,
    "truncated": false,
    "error": null,
    "usage": {
      "input_tokens": 1200,
      "output_tokens": 350,
      "total_input_tokens": 5400,
      "total_output_tokens": 1200,
      "total_tokens": 6600,
      "cost_usd": 0.0234
    },
    "turn_number": 5,
    "context": {
      "used_tokens": 5400,
      "max_tokens": 200000,
      "pressure": 0.027
    },
    "workspace_status": {
      "branch": "main",
      "dirty": true,
      "ahead": 2,
      "behind": 0
    }
  }
}
```

**Key fields:**
- `content`: the final text response (may be empty if the LLM only used tools).
- `error`: `null` or error string if the turn failed.
- `truncated`: `true` if the LLM hit the output token limit.
- `usage.cost_usd`: estimated cost in USD.
- `context.pressure`: 0.0–1.0 ratio of context window usage.
- `workspace_status`: git info for the working directory.

#### `turn_complete`
Alias — some code paths emit this instead of `result`. Treat identically.

### 5.6 Token counting

#### `out_token`
Incremental output token count (for live cost display).
```json
{
  "type": "out_token",
  "kind": "session",
  "payload": {"count": 15}
}
```

#### `in_token`
Incremental input token count.
```json
{
  "type": "in_token",
  "kind": "session",
  "payload": {"count": 1200}
}
```

#### `token_usage`
Full usage snapshot (legacy, may still be emitted).
```json
{
  "type": "token_usage",
  "kind": "session",
  "payload": {
    "prompt_tokens": 1200,
    "completion_tokens": 350,
    "total_tokens": 1550
  }
}
```

### 5.7 Status

#### `status`
Agent phase changes (for spinner/status bar).
```json
{
  "type": "status",
  "kind": "status",
  "payload": {
    "phase": "tool_use",
    "tool_name": "Bash"
  }
}
```
Phases: `"requesting"`, `"generating"`, `"thinking"`, `"tool_use"`,
`"rate_limited"`, `"waiting"`, `"compacting"`.

### 5.8 Errors

#### `error`
A fatal error occurred during the turn.
```json
{
  "type": "error",
  "kind": "error",
  "payload": {
    "error": "Rate limit exceeded",
    "fatal": true
  }
}
```

### 5.9 Approval

#### `approval_request`
The agent wants to execute a risky tool and needs user confirmation.
**Fans out to both the session room AND the user room.**
```json
{
  "type": "approval_request",
  "kind": "approval",
  "payload": {
    "request_id": "uuid-here",
    "agent_id": "main",
    "user_id": "alice",
    "app_id": "my-app",
    "session_id": "sess-1234",
    "tool_name": "Bash",
    "tool_params": {"command": "rm -rf /tmp/old"},
    "risk_level": "high",
    "description": "Delete temporary files",
    "created_at": 1713178991.5
  }
}
```

**To approve/deny**, call the HTTP endpoint:
```
POST /api/apps/{app_id}/sessions/{session_id}/approval/{request_id}
Body: {"approved": true}  // or false
```

### 5.10 Background / Notifications

#### `notification`
A background activation event.
```json
{
  "type": "notification",
  "kind": "background_activation",
  "payload": {
    "source": "cron",
    "trigger": "daily-report"
  }
}
```

#### `notification_result`
A background activation completed.
```json
{
  "type": "notification_result",
  "kind": "background_activation",
  "payload": {
    "source": "cron",
    "result": "success"
  }
}
```

### 5.11 Hooks

#### `hook`
A hook executed (for debug/dev UIs).
```json
{
  "type": "hook",
  "kind": "session",
  "payload": {
    "event": "tool_end",
    "action": "inject_message",
    "hook_name": "auto-lint"
  }
}
```

#### `hook_notification`
A hook notification (user-visible).
```json
{
  "type": "hook_notification",
  "kind": "session",
  "payload": {
    "title": "Lint warning",
    "message": "3 issues found",
    "level": "warning"
  }
}
```

### 5.12 Abort

#### `abort`
The user aborted the running turn (via `POST /{app_id}/sessions/{sid}/abort`).
```json
{
  "type": "abort",
  "kind": "session",
  "payload": {}
}
```
(Note: `session_id` is in the envelope, not the payload.)

### 5.13 Preview (live canvas / app shells)

Preview events are emitted by apps that load the `preview` module (e.g.
digitorn-builder, react-sandbox, slide editors). They are prefixed with
`preview:` to distinguish them from agent events. All preview events have
`kind: "session"` and arrive on the `session:{sid}` room.

#### `preview:state_changed`
A single scalar changed in the session's state map.
```json
{
  "type": "preview:state_changed",
  "kind": "session",
  "payload": {"key": "current_step", "value": "build", "preview_seq": 3}
}
```

#### `preview:state_patched`
Multiple state fields merged.
```json
{
  "type": "preview:state_patched",
  "kind": "session",
  "payload": {"patch": {"progress": 75, "title": "Building..."}, "preview_seq": 4}
}
```

#### `preview:resource_set`
A resource upserted into a named channel (nodes, edges, files, slides...).
```json
{
  "type": "preview:resource_set",
  "kind": "session",
  "payload": {
    "channel": "nodes",
    "id": "auth-module",
    "payload": {"label": "Auth Module", "type": "state", "status": "running"},
    "preview_seq": 5
  }
}
```

#### `preview:resource_patched`
Partial update to an existing resource.
```json
{
  "type": "preview:resource_patched",
  "kind": "session",
  "payload": {
    "channel": "nodes", "id": "auth-module",
    "patch": {"status": "done"},
    "payload": {"label": "Auth Module", "status": "done"},
    "preview_seq": 6
  }
}
```

#### `preview:resource_deleted`
A resource removed from a channel.
```json
{
  "type": "preview:resource_deleted",
  "kind": "session",
  "payload": {"channel": "edges", "id": "auth->db", "preview_seq": 7}
}
```

#### `preview:resource_bulk_set`
Many resources upserted in one event (snapshot/import).
```json
{
  "type": "preview:resource_bulk_set",
  "kind": "session",
  "payload": {
    "channel": "files",
    "items": {"main.py": {"content": "..."}, "app.css": {"content": "..."}},
    "replace": true,
    "preview_seq": 8
  }
}
```

#### `preview:channel_cleared`
All resources in a channel removed.
```json
{
  "type": "preview:channel_cleared",
  "kind": "session",
  "payload": {"channel": "nodes", "preview_seq": 9}
}
```

#### `preview:cleared`
Entire preview state reset.
```json
{
  "type": "preview:cleared",
  "kind": "session",
  "payload": {"preview_seq": 10}
}
```

#### `preview:snapshot`
Full state replay (on reconnect or explicit request).
```json
{
  "type": "preview:snapshot",
  "kind": "session",
  "payload": {
    "state": {"mode": "edit"},
    "resources": {"nodes": {...}, "edges": {...}},
    "preview_seq": 0
  }
}
```

### 5.14 Widget (declarative UI)

Widget events are emitted by apps that load the `widget` module. Prefixed
with `widget:`. All have `kind: "session"`.

#### `widget:render`
A widget rendered or replaced in a zone.
```json
{
  "type": "widget:render",
  "kind": "session",
  "payload": {
    "widget_id": "status-card",
    "zone": "workspace",
    "target": "main",
    "tree": {"type": "card", "props": {"title": "Build Status"}},
    "widget_seq": 1
  }
}
```

#### `widget:update`
Partial update to an existing widget.
```json
{
  "type": "widget:update",
  "kind": "session",
  "payload": {
    "widget_id": "status-card",
    "patch": {"title": "Done!"},
    "state": {"progress": 100},
    "widget_seq": 2
  }
}
```

#### `widget:close`
Widget closed/removed.
```json
{
  "type": "widget:close",
  "kind": "session",
  "payload": {"widget_id": "status-card", "widget_seq": 3}
}
```

#### `widget:error`
Widget error.
```json
{
  "type": "widget:error",
  "kind": "session",
  "payload": {
    "widget_id": "chart-1",
    "binding": "data-source",
    "message": "No data available",
    "widget_seq": 4
  }
}
```

#### `widget:state`
Global widget state update.
```json
{
  "type": "widget:state",
  "kind": "session",
  "payload": {"state": {"form": {"name": "test"}}, "widget_seq": 5}
}
```

#### `widget:cleared`
All widgets cleared.
```json
{
  "type": "widget:cleared",
  "kind": "session",
  "payload": {"widget_seq": 6}
}
```

### 5.15 Workspace files (replaces the removed workbench)

The workbench system has been removed. Workspace file mutations are now
emitted as `preview:resource_set` / `preview:resource_patched` / `preview:resource_deleted`
events on channel `"files"`. See section 5.7 (Preview events) for the full schema.

Each file payload includes: `content`, `language`, `size`, `lines`, `status`
(`added`/`modified`/`deleted`), `operation`, `insertions`, `deletions`,
`total_insertions`, `total_deletions`, `diff`, `unified_diff`, `updated_at`.

### 5.16 Credentials

#### `credential_required`
The agent needs a credential to continue.
```json
{
  "type": "credential_required",
  "kind": "session",
  "payload": {
    "code": "credential_required",
    "provider": "openai",
    "message": "API key needed for OpenAI"
  }
}
```

### 5.17 System

#### `connected`
Handshake event (see §1). Only emitted once on connect, directly to the
connecting client (not broadcast to a room).

---

## 6. Complete Kind Map

```
# Agent events
token                    → session
stream_done              → session
thinking                 → session
thinking_started         → session
thinking_delta           → session
tool_start               → session
tool_call                → session
memory_update            → session
agent_event              → session
hook                     → session
hook_notification        → session
terminal_output          → session
out_token                → session
in_token                 → session
result                   → session
turn_complete            → session
abort                    → session

# Preview events
preview:state_changed    → session
preview:state_patched    → session
preview:resource_set     → session
preview:resource_patched → session
preview:resource_deleted → session
preview:resource_bulk_set→ session
preview:channel_cleared  → session
preview:cleared          → session
preview:snapshot         → session

# Widget events
widget:render            → session
widget:update            → session
widget:close             → session
widget:error             → session
widget:state             → session
widget:cleared           → session
widget:snapshot          → session

# Workbench events

# Credentials
credential_required      → session
credential_auth_required → session

# Other
error                    → error
approval_request         → approval
notification             → background_activation
notification_result      → background_activation
status                   → status
connected                → system
unknown/other            → session (default)
```

---

## 7. Reconnection Strategy

### On disconnect:
1. Store `lastSeq` from the last event you received.
2. Client library should auto-reconnect (with exponential backoff).

### On reconnect:
1. Receive `connected` handshake → compare `latest_seq` with your `lastSeq`.
2. If `latest_seq == lastSeq`: nothing missed, carry on.
3. If `latest_seq > lastSeq`: events were published while offline.
4. If `latest_seq < lastSeq`: **daemon restarted** (seq reset). Do a full refresh
   (reload session history via HTTP `GET /api/apps/{app_id}/sessions/{sid}/history`).

### Catch up after gap:
```dart
// Option A: join_session with since (recommended for session-scoped catch-up)
final ack = await socket.emitWithAck('join_session', {
  'app_id': appId,
  'session_id': sessionId,
  'since': lastSeq,
});

// Option B: explicit replay (for broader catch-up)
final ack = await socket.emitWithAck('replay', {
  'since': lastSeq,
  'session_id': sessionId,  // optional filter
});
```

### Buffer limits:
- Server keeps max **2000 events per user** in a ring buffer.
- If the client was offline for too long and events were evicted,
  `replay` returns what's available. The client should detect the gap
  (first replayed seq > lastSeq + 1) and do a full refresh.

---

## 8. Typical Client Flow

```
1. Connect to /events with JWT
   ← receive "connected" {latest_seq, user_id}

2. Auto-joined to user:{user_id} room
   ← user-level events (notifications, approvals) start flowing

3. User opens an app's session list
   → HTTP GET /api/apps/{app_id}/sessions
   → join_app({app_id})
   ← app-level events start flowing

4. User taps a session
   → join_session({app_id, session_id, since: lastSeq})
   ← missed events replayed, then live events flow
   ← ack contains latest_seq

5. User sends a message
   → send_message({app_id, session_id, message, workspace?})
   ← ack = {ok: true, accepted: true}
   ← token events stream in (append deltas to text buffer)
   ← tool_start / tool_call events (show tool execution UI)
   ← result event (turn complete, show final response)

6. User navigates away from session
   → leave_session({session_id})

7. User navigates away from app
   → leave_app({app_id})

8. User approves a tool call
   → HTTP POST /api/apps/{app_id}/sessions/{sid}/approval/{request_id}
     body: {"approved": true}

9. User aborts a running turn
   → HTTP POST /api/apps/{app_id}/sessions/{sid}/abort
   ← abort event arrives on session room

10. Connection drops
    → auto-reconnect with stored JWT
    ← "connected" handshake
    → join_session with since=lastSeq to catch up
```

---

## 9. HTTP REST Endpoints (still needed)

Socket.IO replaces SSE for **agent session events only**. These HTTP
endpoints are still required:

### REST (JSON)

| Method | Path | Use |
|--------|------|-----|
| `GET` | `/api/apps` | List deployed apps |
| `POST` | `/api/apps/{app_id}/sessions` | Create new session |
| `GET` | `/api/apps/{app_id}/sessions` | List sessions |
| `GET` | `/api/apps/{app_id}/sessions/{sid}` | Get session metadata |
| `GET` | `/api/apps/{app_id}/sessions/{sid}/history` | Full message history |
| `DELETE` | `/api/apps/{app_id}/sessions/{sid}` | Delete session |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/messages` | Send message (HTTP alternative to Socket.IO `send_message`) |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/abort` | Abort running turn |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/approval/{rid}` | Approve/deny tool |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/compact` | Compact context |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/fork` | Fork session |
| `POST` | `/api/apps/{app_id}/sessions/{sid}/undo` | Undo last turn |
| `GET` | `/api/apps/{app_id}/sessions/{sid}/export` | Export session |
| `GET` | `/api/users/me/inbox` | User inbox |
| `GET` | `/api/users/me/inbox/unread_count` | Unread count |
| `GET` | `/api/users/me/usage` | Token usage stats |
| `GET` | `/api/users/me/profile` | User profile |
| `GET` | `/api/credentials/providers` | LLM provider catalog |
| `POST` | `/api/credentials` | Create credential |
| `GET` | `/api/discovery/modules` | Module catalog |

### SSE Streams (DEPRECATED — do NOT use)

These SSE endpoints still exist for backward compat but are **deprecated**.
All their events now flow through Socket.IO (see §5.13–5.15). Flutter clients
should NOT open these connections.

| Method | Path | Status |
|--------|------|--------|
| `GET` | `/api/apps/{app_id}/sessions/{sid}/preview-events` | **Deprecated** — use `preview:*` events on Socket.IO |
| `GET` | `/api/apps/{app_id}/sessions/{sid}/widget-events` | **Deprecated** — use `widget:*` events on Socket.IO |

> **Zero SSE needed.** A single Socket.IO connection on `/events` carries
> agent + preview + widget events.

---

## 10. Server Config (reference)

```yaml
server:
  host: "127.0.0.1"
  port: 8765
  auth_enabled: true
  jwt_secret: "<at-least-32-chars>"

websocket:
  rate_limit_window: 10.0       # seconds
  rate_limit_max_connections: 30 # per IP within window
  ping_interval: 25             # seconds (keep-alive)
  ping_timeout: 10              # seconds
```
---

## 11. Dart/Flutter Pseudo-code

```dart
import 'package:socket_io_client/socket_io_client.dart' as IO;

class DigitornEventService {
  late IO.Socket _socket;
  int _lastSeq = 0;
  String? _userId;
  final _eventController = StreamController<Map<String, dynamic>>.broadcast();

  Stream<Map<String, dynamic>> get events => _eventController.stream;

  void connect(String url, String jwtToken) {
    _socket = IO.io('$url/events', <String, dynamic>{
      'transports': ['websocket'],
      'auth': {'token': jwtToken},
      'forceNew': true,
      'autoConnect': true,
      'reconnection': true,
      'reconnectionDelay': 1000,
      'reconnectionDelayMax': 30000,
    });

    _socket.on('event', (data) {
      final env = Map<String, dynamic>.from(data);
      final seq = env['seq'] as int? ?? 0;
      if (seq > _lastSeq) _lastSeq = seq;

      if (env['type'] == 'connected') {
        _userId = env['user_id'];
        final serverSeq = env['latest_seq'] as int? ?? 0;
        if (serverSeq < _lastSeq) {
          // Daemon restarted — full refresh needed
          _lastSeq = 0;
        }
      }

      _eventController.add(env);
    });

    _socket.on('disconnect', (_) {
      // Will auto-reconnect. lastSeq is preserved.
    });

    _socket.on('connect', (_) {
      // Handshake will arrive as "connected" event
    });
  }

  Future<Map<String, dynamic>> joinSession(String appId, String sessionId) async {
    final ack = await _socket.emitWithAck('join_session', {
      'app_id': appId,
      'session_id': sessionId,
      'since': _lastSeq,
    });
    return Map<String, dynamic>.from(ack);
  }

  Future<Map<String, dynamic>> leaveSession(String sessionId) async {
    final ack = await _socket.emitWithAck('leave_session', {
      'session_id': sessionId,
    });
    return Map<String, dynamic>.from(ack);
  }

  Future<Map<String, dynamic>> sendMessage(
    String appId, String sessionId, String message,
    {String? workspace, List<Map<String, String>>? images}
  ) async {
    final ack = await _socket.emitWithAck('send_message', {
      'app_id': appId,
      'session_id': sessionId,
      'message': message,
      if (workspace != null) 'workspace': workspace,
      if (images != null) 'images': images,
    });
    return Map<String, dynamic>.from(ack);
  }

  Future<Map<String, dynamic>> replay({String? sessionId, String? appId}) async {
    final ack = await _socket.emitWithAck('replay', {
      'since': _lastSeq,
      if (sessionId != null) 'session_id': sessionId,
      if (appId != null) 'app_id': appId,
    });
    return Map<String, dynamic>.from(ack);
  }

  void disconnect() {
    _socket.disconnect();
    _eventController.close();
  }
}
```

---

## 12. Test Coverage (verified)

All of the above has been tested with **91 automated tests** (65 unit + 26 integration):

- ✅ Connect/handshake with `latest_seq` + `capabilities`
- ✅ Auto-join `user:{uid}` room, user-level events received
- ✅ `join_session` ownership check (denied for wrong user)
- ✅ `join_session` with `since` → replay missed events
- ✅ Session room receives `token`, `tool_start`, `tool_call`, `result`
- ✅ `approval_request` fans out to session + user rooms
- ✅ `join_app` / `leave_app` / `leave_session` room management
- ✅ `send_message` dispatches agent turn, events flow back
- ✅ Full agent turn with mock LLM (token→tool_call→result over wire)
- ✅ Reconnection replay catches all missed events
- ✅ JWT auth: valid token accepted, missing/bad token rejected
- ✅ Multi-user isolation: Alice's events don't reach Bob
- ✅ Cross-user session join denied
- ✅ `replay` on-demand with session filter
- ✅ `latest_seq` query
- ✅ In-process handler receives every event (inbox producer)
- ✅ Notification poller running at daemon boot
- ✅ `ping`/`pong` health check
