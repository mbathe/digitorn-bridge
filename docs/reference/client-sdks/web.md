# Digitorn Web Client - Complete Specification

## Overview

Build a complete web client for the Digitorn AI Agent Platform.
The backend daemon exposes a REST API + Socket.IO on namespace `/events` (not SSE).
The client must handle ALL features end-to-end.

**Tech stack**: React 19 + TypeScript + Tailwind CSS + Vite
**Design**: Dark mode first, clean, modern (inspired by ChatGPT, Cursor, Genspark)
**State management**: Zustand or Jotai
**Icons**: Lucide React
**Code blocks**: Monaco Editor (read-only) or Shiki for syntax highlighting
**Markdown**: react-markdown + rehype-highlight

---

## Daemon Connection

Base URL: configurable, default `http://127.0.0.1:8000`

All API calls use:
```
Authorization: Bearer {access_token}
Content-Type: application/json
```

Standard response format:
```json
{"success": true, "data": {...}, "error": null}
```

---

## Authentication (8 routes)

```
POST /auth/login          {username, password} → {access_token, refresh_token}
POST /auth/register       {username, password, email} → {access_token}
POST /auth/refresh        {refresh_token} → {access_token, refresh_token}
POST /auth/logout         {} → 200
GET  /auth/me             → {id, username, email, roles, permissions}
```

### Login Screen
- Centered card, logo on top
- Username + password fields
- "Login" button (primary)
- "Create account" link → register form
- Error message display (red banner)
- Auto-redirect to home on success
- Store tokens in localStorage, auto-refresh before expiry

---

## Pages & Navigation

### Sidebar (72px, permanent, dark)

```
┌──────┐
│  🔷  │  Logo → Home
│  ⊞   │  New (menu: New Chat, Deploy App)
│  💬  │  Digitorn Chat (builtin, always visible)
│  📋  │  Apps list
│  📁  │  Recent sessions
│      │
│  ⚙   │  Settings
│  ●   │  User avatar
└──────┘
```

### Page: Home (no session active)

```
┌─────────────────────────────────────────┐
│          💬 Digitorn Chat               │
│       Start a conversation              │
│                                         │
│  ┌──────────────────────────────────┐   │
│  │ Type your message...          ➤  │   │
│  └──────────────────────────────────┘   │
│                                         │
│  [💡 Explain] [🔍 Search] [✍️ Write]    │  ← quick_prompts
│                                         │
│  ── Your Apps ──────────────────────    │
│  ┌──────┐ ┌──────┐ ┌──────┐            │
│  │ 💻   │ │ 🔍   │ │ 📊   │            │  ← deployed apps
│  │OpenCo│ │Review│ │Data  │            │
│  └──────┘ └──────┘ └──────┘            │
└─────────────────────────────────────────┘
```

Data source: `GET /api/apps` returns:
```json
[{
  "app_id": "digitorn-chat",
  "name": "Digitorn Chat",
  "icon": "💬",
  "color": "#4f8cff",
  "category": "general",
  "mode": "conversation",
  "builtin": true,
  "greeting": "Hello! I'm Digitorn Chat...",
  "quick_prompts": [
    {"label": "Explain something", "icon": "💡", "message": "Explain..."},
    {"label": "Search the web", "icon": "🔍", "message": "Search..."}
  ],
  "description": "Your AI assistant",
  "tags": ["chat", "general"]
}]
```

- `builtin: true` apps appear in the hero section, not in "Your Apps"
- App cards: colored circle (from `color`) + emoji icon + name
- Click app card → open last session or create new

---

### Page: Chat (session active)

Layout: 3 columns (sidebar 72px | chat center | panel right optional)

```
┌────────┬────────────────────────┬──────────────┐
│Sidebar │  Header                │              │
│        │  ─────────────────────  │   Panel      │
│        │  [messages...]          │   (optional) │
│        │  [tool calls...]        │              │
│        │  [thinking blocks...]   │   File tree  │
│        │                         │   or         │
│        │  [approval banner]      │   Code view  │
│        │  [error banner]         │   or         │
│        │  ┌──────────────────┐   │   Plan view  │
│        │  │ Message...    ➤  │   │              │
│        │  └──────────────────┘   │              │
└────────┴────────────────────────┴──────────────┘
```

#### Header
- App name (bold) + Session title (light)
- Right side: 🤖 sub-agent count | 📊 context meter | ⋮ menu

#### Sending messages

```
POST /api/apps/{appId}/sessions/{sessionId}/messages
Body: {"message": "...", "workspace": "/path/to/project"}
Response: {"success": true, "data": {"session_id": "...", "status": "accepted"}}
```

Returns 202 immediately. Events arrive via Socket.IO on the `/events` namespace.

#### Receiving events (Socket.IO)

Connect once to the `/events` namespace on Socket.IO (WebSocket transport).
Subscribe to a session via `join_session`. Events arrive as `event` envelopes
on the same connection.

```javascript
import { io } from "socket.io-client";

const socket = io(`ws://${host}/events?token=${encodeURIComponent(jwt)}`, {
  transports: ["websocket"],
  auth: { token: jwt },
});

socket.on("connect", () => {
  socket.emit("join_session", {
    app_id: appId,
    session_id: sessionId,
    since: 0, // last seq received - use for replay on reconnect
  });
});

socket.on("event", (envelope) => {
  // envelope = { type, seq, kind, app_id, session_id, payload, ts }
  dispatch(envelope.type, envelope.payload);
});
```

#### ALL Socket.IO Event Types

**1. `token`** - Streaming text delta
```json
{"type": "token", "data": {"delta": "Hello, "}}
```
→ Append delta to current assistant message (streaming effect)

**2. `tool_start`** - Tool execution started
```json
{"type": "tool_start", "data": {
  "id": "call_abc", "name": "filesystem__edit", "params": {"path": "src/app.tsx"},
  "label": "Edit", "detail": "src/app.tsx"
}}
```
→ Show spinner with tool name + detail

**3. `tool_call`** - Tool execution completed
```json
{"type": "tool_call", "data": {
  "id": "call_abc", "name": "filesystem__edit",
  "success": true, "error": "",
  "result": {"path": "...", "diff": "...", "lint": {...}},
  "diff": "@@ -42,3 +42,5 @@\n-old\n+new",
  "previous_content": "... old file content ...",
  "new_content": "... new file content ...",
  "label": "Edit", "detail": "src/app.tsx"
}}
```
→ Replace spinner with tool result card. Show diff if available.
→ `previous_content` + `new_content` = compute full diff with deletions (red) + additions (green)

**4. `thinking_started`** - Model started reasoning
```json
{"type": "thinking_started", "data": {}}
```
→ Show collapsible "Thinking..." block with pulse animation

**5. `thinking_delta`** - Reasoning text streaming
```json
{"type": "thinking_delta", "data": {"delta": "Let me analyze..."}}
```
→ Append to thinking block (italic, muted color)

**6. `thinking`** - Complete thinking block
```json
{"type": "thinking", "data": {"text": "Full reasoning text..."}}
```
→ Replace streaming thinking with final text. Auto-collapse.

**7. `terminal_output`** - Shell command output
```json
{"type": "terminal_output", "data": {"stdout": "...", "stderr": "..."}}
```
→ Dark terminal block. Green text for stdout, red for stderr. Monospace font.

**8. `memory_update`** - Agent updated its memory
```json
{"type": "memory_update", "data": {"action": "set_goal", "result": {...}}}
```
→ Silent (no UI). Update internal memory state if tracking.

**9. `agent_event`** - Sub-agent lifecycle
```json
{"type": "agent_event", "data": {
  "action": "spawn_agent",
  "result": {"agent_id": "agent_abc", "task": "Find usages", "status": "running"}
}}
```
→ Update sub-agent counter in header. Actions: spawn_agent, agent_result, agent_cancel.

**10. `status`** - Agent phase change
```json
{"type": "status", "data": {"phase": "requesting"}}
```
Phases: `requesting`, `generating`, `thinking`, `tool_use`, `rate_limited`, `waiting`
→ Update spinner text to match phase.

**11. `result`** - Turn completed
```json
{"type": "result", "data": {
  "content": "Here's what I found...",
  "session_id": "...",
  "tool_calls_count": 3,
  "turns_used": 2,
  "error": null,
  "usage": {"input_tokens": 1200, "output_tokens": 350, "cost_usd": 0.003},
  "context": {
    "max_tokens": 200000, "effective_max": 195904,
    "total_estimated_tokens": 15580, "pressure": 0.08,
    "available_tokens": 180324, "compactions": 0,
    "system_prompt_tokens": 1650, "tools_schema_tokens": 1530,
    "message_history_tokens": 12400
  }
}}
```
→ Finalize message. Stop spinner. Update context meter. Enable input.

**12. `error`** - Structured error
```json
{"type": "error", "data": {
  "error": "Insufficient balance",
  "code": "insufficient_balance",
  "category": "billing",
  "retry": false,
  "detail": "Error 402..."
}}
```
Error codes: `insufficient_balance`, `auth_error`, `rate_limited`, `context_overflow`, `network_error`, `provider_error`, `permission_denied`, `session_busy`, `internal_error`
→ Show error banner above input. Color by category. "Retry" button if `retry: true`.

**13. `abort`** - Turn was aborted by user
```json
{"type": "abort", "data": {"session_id": "..."}}
```
→ Stop spinner, show "Interrupted", enable input.

**14. `approval_request`** - Agent needs user confirmation
```json
{"type": "approval_request", "data": {
  "request_id": "uuid",
  "tool_name": "filesystem.write",
  "tool_params": {"path": "/config.json", "content": "..."},
  "risk_level": "high",
  "description": "Write to /config.json",
  "created_at": 1712412000.5
}}
```
Two types:
- `tool_name != "ask_user"` → Tool approval (Approve/Reject buttons)
- `tool_name == "ask_user"` → Agent question (question in `tool_params.question`, optional plan in `tool_params.content`)

→ Show approval banner above input. Respond via:
```
POST /api/apps/{appId}/approve
Body: {"request_id": "uuid", "approved": true, "message": ""}
```

**15. `hook`** - Hook fired (compaction, etc.)
```json
{"type": "hook", "data": {
  "hook_id": "_auto_compact", "action_type": "compact_context",
  "phase": "end",
  "details": {"tokens_before": 95000, "tokens_after": 45000, "strategy": "summarize"}
}}
```
→ Show compaction indicator. Update context meter after.

**16. `preview:resource_set`** - Workspace file created or updated (channel `"files"`)
```json
{"type": "preview:resource_set", "payload": {
  "channel": "files",
  "id": "src/App.tsx",
  "payload": {
    "content": "...",
    "language": "tsx",
    "size": 1234,
    "lines": 42,
    "status": "modified",
    "operation": "edit",
    "insertions": 5,
    "deletions": 2,
    "total_insertions": 47,
    "total_deletions": 12,
    "diff": "...",
    "unified_diff": "...",
    "updated_at": 1776297401.5
        {"label": "src/", "level": 0, "icon": "📁"},
        {"label": "app.tsx", "level": 1, "badge": "M", "status": "edit",
         "color": "var(--yellow-text)", "insertions": 15, "deletions": 3}
  }
}}
```
→ Update file tree in right panel. Use `status` (added/modified/deleted) to color file entries. Show total_insertions/total_deletions as a banner.

**17. `stream_done`** - Token streaming finished
```json
{"type": "stream_done", "data": {}}
```
→ Finalize streaming text.

**18. `heartbeat`** - Keepalive
```json
{"type": "heartbeat", "data": {}}
```
→ Ignore (connection alive confirmation).

**19. `in_token` / `out_token`** - Token counts
```json
{"type": "out_token", "data": {"count": 50}}
{"type": "in_token", "data": {"count": 200}}
```
→ Update token counter display (optional).

---

### Page: App Detail

Route: `/apps/{appId}`

```
GET /api/apps/{appId}
GET /api/apps/{appId}/sessions
GET /api/apps/{appId}/triggers    (for background apps)
```

Shows:
- App metadata (name, icon, color, description, mode, tags)
- Sessions list (recent, active)
- For background apps: triggers, background sessions, activations
- Deploy/undeploy buttons (if not builtin)
- Quick prompts

---

### Page: Background App Dashboard

For apps with `mode: "background"`:

```
GET /api/apps/{appId}/triggers                    → trigger list + channels
GET /api/apps/{appId}/background-sessions         → session list
GET /api/apps/{appId}/background-sessions/{id}    → session detail
GET /api/apps/{appId}/activations                 → activation history
GET /api/apps/{appId}/activations/stats           → aggregated stats
GET /api/apps/{appId}/errors                      → recent errors
POST /api/apps/{appId}/triggers/{id}/fire         → manual fire
POST /api/apps/{appId}/background-sessions        → create session
POST /api/apps/{appId}/background-sessions/{id}/pause
POST /api/apps/{appId}/background-sessions/{id}/resume
DELETE /api/apps/{appId}/background-sessions/{id}
```

Dashboard layout:
```
┌─────────────────────────────────────────────────┐
│ Stats: 156 activations | 98% success | 2 errors │
├─────────────────────────────────────────────────┤
│ Triggers        │ Sessions                       │
│ ├ cron: */5min ●│ ├ Alice DS (active)            │
│ ├ webhook: POST │ ├ Bob DevOps (paused)          │
│ └ watch: *.py   │ └ + New Session                │
├─────────────────────────────────────────────────┤
│ Recent Activations                               │
│ ├ 12:05 ● trigger=cron session=Alice 3.2s       │
│ ├ 12:00 ● trigger=cron session=Bob   2.1s       │
│ └ 11:55 ✗ trigger=webhook error=timeout         │
└─────────────────────────────────────────────────┘
```

---

### Page: Settings

- Daemon connection URL
- Theme toggle (dark/light)
- User profile (from GET /auth/me)
- API keys management (GET/PUT/DELETE /api/apps/{appId}/secrets/{key})

---

## UI Components

### 1. Message Bubble

```
┌─ User ────────────────────────────────────────┐
│ Fix the authentication bug in login.py         │
└────────────────────────────────────────────────┘

┌─ Assistant ────────────────────────────────────┐
│ I'll look at the login code...                 │
│                                                │
│ ┌─ 💭 Thinking (click to expand) ───────────┐ │
│ │ Let me analyze the auth flow...            │ │
│ └────────────────────────────────────────────┘ │
│                                                │
│ ┌─ 📖 Read → login.py ──────────────────────┐ │
│ │ 42 lines read                              │ │
│ └────────────────────────────────────────────┘ │
│                                                │
│ ┌─ ✏️ Edit → login.py ──────────────────────┐ │
│ │ -42│  return None                          │ │
│ │ +42│  result = validate(token)             │ │
│ │ +43│  return result                        │ │
│ │ ── Lint: 0 errors ──────────────────────── │ │
│ └────────────────────────────────────────────┘ │
│                                                │
│ ┌─ 🖥 Terminal ──────────────────────────────┐ │
│ │ $ python -m pytest tests/                  │ │
│ │ 12 passed, 0 failed                        │ │
│ └────────────────────────────────────────────┘ │
│                                                │
│ The bug was in the token validation...         │
└────────────────────────────────────────────────┘
```

### 2. Tool Call Card

Collapsible card for each tool execution:
- Header: icon + tool name + detail (e.g., "Edit → src/app.tsx")
- Badge: ✓ green (success) or ✗ red (error)
- Body (collapsed by default): diff view, terminal output, or result data
- For edit/write: show diff with red (deletions) / green (additions) lines

### 3. Diff View

When `previous_content` and `new_content` are available:
- Compute line diff (use `diff` library)
- Show unified diff with line numbers
- Red background for removed lines, green for added
- Context lines (unchanged) in normal color
- Collapsible, max 300px height

### 4. Approval Banner

Positioned above the input, same width:

For tool approval:
```
┌──────────────────────────────────────────────┐
│ ⚠️ Write → config.json                  HIGH │
│ path: /home/user/config.json                 │
│ [Approve] [Reject ▾]                         │
└──────────────────────────────────────────────┘
```

For ask_user:
```
┌──────────────────────────────────────────────┐
│ 💬 Agent asks:                               │
│ "Should I proceed with this plan?"           │
│ 📋 Plan displayed in side panel →            │
│ ┌─────────────────────────────────────────┐  │
│ │ Your response (optional)...             │  │
│ └─────────────────────────────────────────┘  │
│ [Approve] [Reject]                           │
└──────────────────────────────────────────────┘
```

Respond: `POST /api/apps/{appId}/approve`
Body: `{"request_id": "uuid", "approved": true/false, "message": "..."}`

### 5. Error Banner

```
┌──────────────────────────────────────────────┐
│ 💳 Insufficient balance                      │
│ Check your API billing.                      │
│ [OK]                                         │
└──────────────────────────────────────────────┘
```

Colors by category:
- billing → orange
- auth → red
- rate_limit → amber
- network → grey
- provider → deep orange
- security → dark red
- internal → red

### 6. Context Meter

Small icon in header. Hover shows tooltip:
```
Context: 15,580 / 195,904 (8%)
Available: 180,324 tokens
```

Click shows dialog with full breakdown:
- Progress bar segmented by: system prompt (blue), tools (purple), messages (green)
- Numbers for each section
- Compaction count

Data source: `context` field in `result` Socket.IO event.

### 7. File Tree (right panel)

Updated by `preview:resource_set` Socket.IO events on channel `"files"`.

```
📁 src/
  📄 app.tsx        M  +15 -3
  📄 config.json    A  +12
  📄 helpers.ts        (read)
📄 old-file.ts     D  -30
```

Badges: M (yellow), A (green), D (red)
Insertions in green, deletions in red, after the filename.

### 8. Sub-Agent Indicator

In header: `🤖 2` when sub-agents are running.
Hover shows list of active agents with task description.

### 9. Status Spinner

Below the last message during agent turn:
- `requesting` → "Sending to model..." (gray spinner)
- `generating` → "Generating..." (blue dots)
- `thinking` → "Reasoning..." (purple pulse)
- `tool_use` → "Executing: {tool}..." (orange spinner)
- `rate_limited` → "Rate limited (attempt {n})" (yellow)

---

## Session Management

### Create session
Send first message → session auto-created by daemon.
Session ID: generate client-side `crypto.randomUUID()`.

### Load session
```
GET /api/apps/{appId}/sessions/{sessionId}          → metadata, is_active, context
GET /api/apps/{appId}/sessions/{sessionId}/history   → {messages, events, preview_snapshot, memory_snapshot}
```

### Resume interrupted session
```
POST /api/apps/{appId}/sessions/{sessionId}/resume
```
The daemon auto-recovers orphaned tool calls.

### Session actions
```
POST /api/apps/{appId}/sessions/{sessionId}/abort     → stop current turn
POST /api/apps/{appId}/sessions/{sessionId}/compact   → force context compaction
POST /api/apps/{appId}/sessions/{sessionId}/fork      → duplicate session
POST /api/apps/{appId}/sessions/{sessionId}/undo      → undo last edit
DELETE /api/apps/{appId}/sessions/{sessionId}          → delete
GET /api/apps/{appId}/sessions/{sessionId}/export?format=markdown → export
```

---

## Deploy App

```
POST /api/apps/deploy
Body: {"yaml_path": "/path/to/app.yaml", "force": true, "secrets": {"API_KEY": "..."}}
```

Or upload:
```
POST /api/apps/deploy/upload
Body: FormData with YAML file
```

Undeploy:
```
DELETE /api/apps/{appId}
```
Note: builtin apps cannot be undeployed.

---

## Theme

### Dark mode (default)
```css
--bg-primary: #0f0f23;
--bg-secondary: #1a1a2e;
--bg-card: #252542;
--bg-input: #1e1e3a;
--text-primary: #e2e8f0;
--text-secondary: #94a3b8;
--text-muted: #64748b;
--accent: #4f8cff;
--green: #4caf50;
--red: #f44336;
--yellow: #ffc107;
--orange: #ff9800;
--purple: #8b5cf6;
--border: #2d2d4a;
--code-bg: #0d1117;
```

### Light mode
```css
--bg-primary: #fafafa;
--bg-secondary: #ffffff;
--bg-card: #f1f5f9;
--bg-input: #ffffff;
--text-primary: #1a202c;
--text-secondary: #64748b;
--accent: #3b82f6;
--border: #e2e8f0;
--code-bg: #f6f8fa;
```

---

## Responsive

- Desktop (>1200px): sidebar 72px + chat + optional right panel
- Tablet (768-1200px): sidebar collapsed 48px + chat
- Mobile (<768px): bottom tab navigation, no sidebar

---

## Key Implementation Notes

1. **Socket.IO auth**: pass the JWT in the URL query param `?token=...` (browser WebSocket cannot send custom headers). Example: `io("ws://host/events?token=" + encodeURIComponent(jwt), {transports: ["websocket"]})`.

2. **Reconnection**: If Socket.IO disconnects, reconnect and emit `join_session` with `since: N` where N is the last seq received. The daemon replays missed events.

3. **Session persistence**: Store `{appId, sessionId}` in localStorage to resume on page reload.

4. **Optimistic UI**: Show user message immediately before 202 response. Show spinner immediately.

5. **Markdown rendering**: Assistant messages are Markdown. Render with syntax highlighting for code blocks.

6. **File diff**: Use `diff` npm package to compute line-by-line diff from `previous_content` / `new_content`.

7. **Quick prompts**: When clicked, inject `message` into input field (don't send immediately - let user edit/complete).

8. **Abort**: Button visible during active turn. Calls `POST /sessions/{sid}/abort`. Agent state is preserved.

9. **Workspace**: For coding apps (`workspace_mode: required`), prompt user to select a directory before first message. Pass as `workspace` in message body.

10. **Builtin app**: `digitorn-chat` is always first in the list, shown in the hero section of the home page.

---

## Image / Multimodal Handling

### Uploading images

Send images with a message via JSON:

```
POST /api/apps/{appId}/sessions/{sessionId}/messages
{
  "message": "What's in this image?",
  "images": [
    {"data": "iVBOR...base64...", "mime": "image/png", "name": "screenshot.png"}
  ]
}
```

Max 10 images per message, max 10MB each.

### Images in Socket.IO events

When a tool reads or generates an image, the `tool_call` event includes image data:

```json
{
  "type": "tool_call",
  "data": {
    "name": "filesystem__read",
    "result": {"path": "logo.png", "is_image": true, "mime_type": "image/png", "width": 800, "height": 600},
    "image_data": "iVBOR...base64...",
    "image_mime": "image/png"
  }
}
```

Display the image inline in the tool call card when `image_data` is present.

### Lazy loading images

For session history (resume), images are stored server-side:

```
GET /api/apps/{appId}/sessions/{sessionId}/images/{imageId}
Response: raw image bytes (Content-Type: image/png)
```

Use this URL as `<img src="...">` for lazy loading instead of embedding base64 in history JSON.

### Image display in chat

- User images: show thumbnail (max 300px width) in the user bubble
- Tool result images: show in the tool call card (collapsible, max 500px)
- Click to zoom (full resolution in a modal/lightbox)
