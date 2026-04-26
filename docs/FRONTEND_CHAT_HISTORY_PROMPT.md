# Frontend integration — Chat history reconstruction

## Your mission

Build a client that can **fully reconstruct any chat session** from the
daemon's `history_log` ledger. The server guarantees: for any
completed session, every user message, every assistant message, every
tool call, every tool result, every streaming token, every error, every
pending approval, every attachment — **all of it** — is persisted in
the single `history_log` table with a globally-unique monotonic
timestamp. You render.

Two display modes you must support:

1. **Cold open** (user opens an old chat): `GET /history` → render the
   whole timeline.
2. **Warm session** (user is chatting now): same `GET /history` for the
   backlog, **plus** Socket.IO live events grafted on top with a
   `since_seq` watermark to stay in sync.

---

## The canonical endpoint

```
GET  /api/apps/{app_id}/sessions/{session_id}/history
     ?since_seq=0               (int, optional, default 0)
     ?events_limit=50000        (int, optional, default 50000)
     ?include_system=false      (bool, optional — true for debug)
Auth: Bearer <jwt>              (401 if anonymous, 404 if not your session)
```

### Response envelope

```jsonc
{
  "success": true,
  "data": {
    // ── Session metadata (renders the header bar) ────────────
    "session_id": "7eff76ed-...",
    "app_id": "digitorn-chat",
    "user_id": "u123",
    "title": "Discussion about foo",
    "created_at": 1730000000.0,          // unix seconds
    "last_active": 1730001234.5,
    "message_count": 42,                 // user+assistant+tool rows
    "turn_count": 21,
    "interrupted": false,                // true → daemon crashed mid-turn
    "turn_active": false,                // true → an LLM call is in flight RIGHT NOW
    "pending_queue": [                   // messages waiting to run
      { "id": "q-...", "message": "next question", "position": 1 }
    ],

    // ── The structured chat (for rendering bubbles) ──────────
    "messages": [ /* Message[], see below */ ],

    // ── The raw event timeline (for streaming state, tool chips,
    //     thinking, errors, approvals, everything not in messages) ─
    "events": [ /* Event[], see below */ ],

    // ── Pagination cursors (for huge sessions) ───────────────
    "events_total": 420,
    "events_next_seq": 180,              // pass as ?since_seq next call
    "events_has_more": true,

    // ── Optional restoration snapshots ───────────────────────
    "memory_snapshot": { "goal": "...", "todos": [...], "facts": [...] },
    "preview_snapshot": { /* workspace file tree etc. */ }
  }
}
```

---

## `Message` shape — the chat bubbles

The `messages` array is already denormalised for UI rendering: tool
calls are attached to their parent assistant message, tool results are
merged into each tool call, system messages are filtered out unless
`include_system=true`.

```ts
type Message =
  | UserMessage
  | AssistantMessage
  | AssistantToolMessage;   // assistant bubble whose only content is tool invocations

interface UserMessage {
  role: "user";
  content: string | ContentPart[];   // multimodal when array
}

interface AssistantMessage {
  role: "assistant";
  content: string;                   // the final rendered text
  thinking?: string;                 // chain-of-thought, if the provider exposed it
  tool_calls?: ToolCall[];           // snake_case — canonical
  toolCalls?: ToolCall[];            // camelCase duplicate for legacy clients (same data)
}

interface AssistantToolMessage extends AssistantMessage {
  content: "";
  tool_calls: ToolCall[];            // required when content is empty
}

interface ToolCall {
  id: string;                        // correlates with tool result
  name: string;                      // "filesystem.write", "shell.bash", ...
  label: string;                     // server-computed short label for chip
  detail: string;                    // server-computed secondary text
  params: Record<string, any>;       // the args the model passed
  result: any;                       // the tool's return value (parsed JSON when possible)
  status: "done" | "running" | "error" | "pending_approval";
}

// Multimodal user content (images, PDFs, audio description)
type ContentPart =
  | { type: "text"; text: string }
  | { type: "image"; source: { type: "base64"; media_type: string; data: string } }
  | { type: "image_url"; image_url: { url: string } }
  | { type: "file"; name: string; source: { media_type: string; data: string } }
  | { type: "document"; source: { media_type: string; data: string } };
```

### Rendering rules

- `role:"user"` → right-aligned bubble, show text; if `content` is an
  array, render each part: images inline, files as attachment chips.
- `role:"assistant"` with `content` only → left-aligned bubble,
  markdown-rendered.
- `role:"assistant"` with `tool_calls` → render the text (if any),
  then one "tool chip" per call showing `label`, expandable to reveal
  `params`, and below that the `result` (pretty-printed JSON or text).
- `thinking` → collapsible "thought bubble" above the response (use
  `provider: deepseek-reasoner` clients want this visible).

---

## `Event` shape — the raw timeline

Events carry EVERYTHING that's not a message row — streaming deltas,
hook firings, tool lifecycle, errors, approvals, quota hits. Use them
to render live state, re-hydrate mid-turn progress, and surface any UI
state that isn't "a bubble".

```ts
interface Event {
  type: string;                // event type — see the exhaustive list below
  kind: string;                // "session" | "agent" | "system" | "error" | "approval" | "background_activation"
  seq: number;                 // strictly monotonic per session — use for dedup + ordering
  ts: string;                  // ISO-8601 with microsecond precision + tz — globally unique
  app_id: string | null;
  session_id: string | null;
  user_id: string | null;
  correlation_id: string | null;  // groups all events of one turn — key for "which bubble does this belong to"

  // Contract fields — promoted to the envelope top level
  event_id: string;            // "ev-<hex>" — primary dedup key across reconnects/fanout
  op_id: string;               // "op-<hex>" | correlation_id — groups a sub-operation
  op_type: "turn" | "tool" | "agent" | "approval" | "compaction" | "system";
  op_state: "pending" | "running" | "waiting_approval"
          | "completed" | "failed" | "cancelled" | "timeout";
  op_parent_id?: string;       // set for nested ops (tool inside a turn inside an agent)

  payload: Record<string, any>;  // type-specific — see each type below
}
```

### Dedup key — READ THIS

On reconnect you may see the same event twice (ring buffer replay +
live stream + HTTP backfill). Dedup on `event_id`, NOT `seq` — seq
differs between the session room and the user room for fan-out events
(approvals).

---

## The exhaustive event-type catalog

Every type your client may receive. Grouped by concern.

### A. Message lifecycle — drive the chat bubble

| `type` | When | Payload highlights | UI action |
|---|---|---|---|
| `user_message` | User sent a message | `{content, correlation_id}` | Append user bubble + start "thinking" spinner |
| `message_queued` | Queued behind a running turn | `{entry_id, position, queue_depth}` | Show "queued, position N" indicator |
| `message_started` | Turn picked up from queue | `{correlation_id}` | Switch spinner to "processing" |
| `message_merged` | Incoming merged with a running turn | `{into_correlation_id}` | Re-bind the pending bubble |
| `message_replaced` | User re-sent a message (edit in-place) | `{old_correlation_id, new}` | Replace bubble content |
| `message_done` | Turn finished successfully | `{correlation_id}` | Stop spinner, mark bubble done |
| `message_cancelled` | User aborted / queue cleared | `{reason}` | Mark bubble "interrupted" |
| `turn_complete` | Parent-of-children turn (sub-agents) finished | `{tool_calls_total}` | Final close for multi-agent turns |
| `result` | Plain "turn returned a text" | `{content}` | Fallback if no assistant_message row yet |

### B. Streaming deltas — drive the typing animation

| `type` | Payload | Notes |
|---|---|---|
| `token` | `{content, seq}` | Single token or chunk. Append char-by-char. |
| `out_token` / `in_token` | Same shape, telemetry-only | You can ignore unless you want a token counter |
| `assistant_stream_snapshot` | `{content, turn}` | **Full assistant text so far** — preferred for reconnect mid-stream. Read this if you arrive late and want the partial message without stitching tokens. |
| `thinking_started` | `{turn}` | Open the "thought bubble" container |
| `thinking_delta` | `{delta}` | Append to the thought bubble |
| `thinking` | `{content}` | Full thinking block (final) |
| `stream_done` | `{turn, total_tokens}` | Stream phase ended — server is finalizing |

### C. Tool lifecycle — drive the tool chips

| `type` | Payload | UI action |
|---|---|---|
| `tool_start` | `{tool, tool_name, params, call_id, op_id}` | Insert a placeholder chip with spinner |
| `tool_call` | `{tool, success, duration_ms, op_id, op_state}` | Flip chip to done/failed, show result |

### D. Agent spawning (for sub-agent apps)

| `type` | Payload |
|---|---|
| `agent_event` | Generic wrapper — see `payload.type` (spawn_agent / agent_progress / agent_result / agent_cancel) |
| `spawn_agent` | `{agent_id, specialist, task}` |
| `agent_progress` | `{agent_id, duration_seconds, tool_calls_count, preview}` |
| `agent_result` | `{agent_id, result_summary, error}` |
| `agent_cancel` | `{agent_id, reason, duration_seconds}` |

### E. Hooks — ambient state updates

| `type` | Payload |
|---|---|
| `hook` | `{event, action_taken, condition_met}` — often silent to the UI but useful for audit views |
| `hook_notification` | User-facing notification raised by a hook |

### F. Memory — sidebar state

| `type` | Payload |
|---|---|
| `memory_update` | `{goal?, todos?, facts?}` — patch, not full replace |

### G. Preview / Workspace — live file state

| `type` | Payload |
|---|---|
| `preview:state_changed` | Full state replace |
| `preview:state_patched` | JSON-patch delta |
| `preview:resource_set` | `{channel, id, payload}` — e.g. `files` channel update |
| `preview:resource_patched` | `{channel, id, patch}` |
| `preview:resource_deleted` | `{channel, id}` |
| `preview:resource_bulk_set` | `{channel, items, replace}` |
| `preview:channel_cleared` | `{channel}` |
| `preview:snapshot` | Fresh full snapshot of all resources |
| `preview:cleared` | Full wipe |

### H. Widgets

| `type` | Payload |
|---|---|
| `widget:render` | `{widget_id, schema, data}` |
| `widget:update` | `{widget_id, patch}` |
| `widget:close` | `{widget_id}` |
| `widget:error` | `{widget_id, error}` |
| `widget:state` | `{widget_id, state}` |
| `widget:cleared` | — |
| `widget:snapshot` | full widgets state |

### I. Approvals — human-in-the-loop modal

| `type` | Payload | UI action |
|---|---|---|
| `approval_request` | `{approval_id, action_id, plan_id, params, reason}` | Open approval modal |
| `credential_required` | `{provider, field, required}` | Open credential picker |
| `credential_auth_required` | `{provider, auth_url}` | Open OAuth flow |

### J. Errors — the structured classification

All error events carry a `category` in `payload` that you **switch on**
to render the right UI.

```ts
interface ErrorPayload {
  error: string;           // human-readable, ready to show
  code: string;            // machine-readable, for analytics + switch
  category:
    | "billing"            // API provider ran out of funds
    | "quota"              // daemon rate limit hit (5h window, etc.)
    | "auth"               // 401/403/token expired
    | "rate_limit"         // provider 429 / overloaded
    | "provider"           // model_not_found / bad_request / 5xx / context_overflow
    | "network"            // connect / DNS / SSL / stream interrupted
    | "timeout"            // tool or LLM took too long (distinct from network)
    | "content_filter"     // provider safety rejected the prompt or output
    | "approval"           // waiting on user approval
    | "validation"         // bad params / Pydantic / IML error
    | "concurrency"        // session busy
    | "security"           // permission / policy violation
    | "storage"            // DB disk full / locked
    | "tool"               // action/worker execution failure
    | "cancelled"          // user aborted
    | "internal";          // fallback — SHOULD be rare
  retry: boolean;          // whether to show a retry button
  detail: string;          // raw error text for debug panel

  // Quota-specific fields (when category="quota")
  retry_after_seconds?: number;
  scope?: string;          // "user:abc" | "app:foo" | "session:..."
  metric?: string;         // "requests" | "tokens" | "cost_usd"
  window?: string;         // "5h" | "1d" | ...
  current?: number;
  limit?: number;

  // Approval-specific fields (when category="approval")
  action_id?: string;
  plan_id?: string;
}
```

UI mapping (recommended):

- `billing` → red banner "Top up your API key" with link
- `quota` → amber banner "Usage limit: X/Y in Z window, retry in Ts"
- `auth` → toast → redirect to credential picker
- `rate_limit` → auto-retry with exponential backoff + "Slow down" toast
- `provider` → inline "Provider error: <human message>" with retry
- `network` → inline with retry, auto-retry 3x
- `timeout` → inline "Took too long" with retry
- `content_filter` → red "This content was flagged by the safety system" (no retry)
- `approval` → modal
- `validation` → highlight the offending fields from `payload.errors[]`
- `concurrency` → disable composer until turn completes
- `security` → red banner "Permission denied"
- `storage` → full-screen error, contact ops
- `tool` → show in the tool chip as failed, offer retry
- `cancelled` → no banner, just mark the turn as "you interrupted this"
- `internal` → generic toast + report-issue button

### K. Abort / status

| `type` | Payload |
|---|---|
| `abort` | `{reason, correlation_id}` — user pressed abort |
| `status` | Freeform status changes |
| `notification` | Freeform background notifications |
| `notification_result` | Freeform completion markers |
| `terminal_output` | Shell-output from background runners |
| `bg_task_update` | Background task state transition |

---

## Reconstruction algorithm (cold open)

```ts
async function loadChat(appId: string, sessionId: string) {
  // 1. Fetch.
  const { data } = await api.get(
    `/api/apps/${appId}/sessions/${sessionId}/history`
  );

  // 2. Seed the session metadata.
  state.title = data.title;
  state.created_at = data.created_at;
  state.turn_active = data.turn_active;
  state.pending_queue = data.pending_queue;

  // 3. Seed the chat bubbles from `messages[]`.
  //    The server already grouped tool_calls + results per assistant msg.
  //    Just render.
  state.bubbles = data.messages.map(renderBubble);

  // 4. Replay the event stream to rebuild live-only UI state.
  const seen = new Set<string>();              // event_id → dedup
  for (const ev of data.events) {
    if (seen.has(ev.event_id)) continue;
    seen.add(ev.event_id);
    applyEvent(state, ev);                     // reducer below
  }

  // 5. Remember the high-water mark — live events arriving on
  //    Socket.IO will be deduped against this.
  state.last_seq = Math.max(
    0, ...data.events.map(e => e.seq)
  );

  // 6. If turn_active, DON'T clear the spinner — a live event stream
  //    will complete it. Attach the live listener now.
  if (data.turn_active) attachLiveStream(sessionId, state.last_seq);

  // 7. Snapshots (optional — only for workspace / memory / preview apps).
  if (data.memory_snapshot) applyMemorySnapshot(state, data.memory_snapshot);
  if (data.preview_snapshot) applyPreviewSnapshot(state, data.preview_snapshot);
}
```

The **event reducer**:

```ts
function applyEvent(state, ev) {
  switch (ev.type) {
    case "user_message":
      // The message bubble is already in state.bubbles from step 3.
      // If it's NOT (we're ahead of the /history snapshot), add it.
      ensureBubble(state, "user", ev.payload, ev.correlation_id);
      break;

    case "message_queued":
      state.pending_queue.push({
        correlation_id: ev.correlation_id,
        position: ev.payload.position,
        message: ev.payload.message,
      });
      break;

    case "message_started":
      state.current_turn = ev.correlation_id;
      state.spinner = "generating";
      break;

    case "token":
    case "assistant_stream_snapshot":
      // Append (or replace, for snapshot) the streaming text onto the
      // in-progress assistant bubble for this correlation_id. If no
      // bubble yet, create one with streaming=true.
      const b = ensureStreamingBubble(state, ev.correlation_id);
      if (ev.type === "token") {
        b.content += ev.payload.content || "";
      } else {
        b.content = ev.payload.content || b.content;
      }
      break;

    case "thinking_started":
      state.thinking = { correlation_id: ev.correlation_id, content: "" };
      break;

    case "thinking_delta":
      if (state.thinking?.correlation_id === ev.correlation_id) {
        state.thinking.content += ev.payload.delta || "";
      }
      break;

    case "thinking":
      // Final thinking block — attach to the bubble.
      const tb = findBubble(state, ev.correlation_id, "assistant");
      if (tb) tb.thinking = ev.payload.content;
      state.thinking = null;
      break;

    case "tool_start":
      const bubble = ensureStreamingBubble(state, ev.correlation_id);
      const call = {
        id: ev.payload.call_id || ev.op_id,
        name: ev.payload.tool || ev.payload.tool_name,
        params: ev.payload.params,
        status: "running",
      };
      bubble.tool_calls = [...(bubble.tool_calls || []), call];
      break;

    case "tool_call":
      // The tool finished — update the placeholder chip.
      updateToolCall(state, ev.op_id, {
        status: ev.payload.success ? "done" : "error",
        duration_ms: ev.payload.duration_ms,
        result: ev.payload.result,
        error: ev.payload.error,
      });
      break;

    case "message_done":
      // Mark the assistant bubble as complete. Clear spinner if this
      // is the current turn.
      const mb = findBubble(state, ev.correlation_id, "assistant");
      if (mb) mb.streaming = false;
      if (state.current_turn === ev.correlation_id) {
        state.current_turn = null;
        state.spinner = null;
      }
      break;

    case "message_cancelled":
    case "abort":
      const cb = findBubble(state, ev.correlation_id, "assistant");
      if (cb) { cb.streaming = false; cb.interrupted = true; }
      state.spinner = null;
      state.current_turn = null;
      break;

    case "error":
      // Render banner / toast based on category. Attach to the
      // current bubble so the user sees where in the conversation
      // it failed.
      state.latest_error = ev.payload;
      renderErrorUI(ev.payload);        // see category table above
      break;

    case "credential_required":
    case "credential_auth_required":
      openCredentialPicker(ev.payload);
      break;

    case "approval_request":
      state.pending_approvals.push(ev.payload);
      openApprovalModal(ev.payload);
      break;

    case "agent_event":
    case "spawn_agent":
    case "agent_progress":
    case "agent_result":
    case "agent_cancel":
      applyAgentEvent(state, ev);
      break;

    case "memory_update":
      applyMemoryPatch(state, ev.payload);
      break;

    case "preview:resource_set":
    case "preview:resource_patched":
    case "preview:resource_deleted":
    case "preview:resource_bulk_set":
    case "preview:channel_cleared":
    case "preview:snapshot":
    case "preview:state_changed":
    case "preview:state_patched":
    case "preview:cleared":
      applyPreviewEvent(state, ev);
      break;

    case "widget:render":
    case "widget:update":
    case "widget:close":
    case "widget:error":
    case "widget:state":
    case "widget:cleared":
    case "widget:snapshot":
      applyWidgetEvent(state, ev);
      break;

    // Telemetry / silent-to-UI
    case "hook":
    case "out_token":
    case "in_token":
    case "token_usage":
    case "stream_done":
    case "turn_complete":
    case "status":
    case "notification":
    case "notification_result":
    case "bg_task_update":
    case "terminal_output":
      // Log or expose in a dev panel, but don't affect the chat bubbles.
      break;

    default:
      console.warn("Unknown event type", ev.type, ev);
  }
}
```

---

## Live streaming (Socket.IO)

```ts
function attachLiveStream(sessionId, sinceSeq) {
  const sio = io(`${BASE}/events`, { auth: { token } });
  sio.emit("join_session", {
    app_id: APP_ID,
    session_id: sessionId,
    since_seq: sinceSeq,
  });
  sio.on("event", (envelope) => {
    if (seen.has(envelope.event_id)) return;
    seen.add(envelope.event_id);
    state.last_seq = Math.max(state.last_seq, envelope.seq);
    applyEvent(state, envelope);
  });
}
```

On reconnect (tab wakes, network recovers):
1. Fetch `/history?since_seq=<state.last_seq>` — backfill any events
   the daemon emitted while you were offline.
2. Re-emit `join_session` with the same `since_seq` — Socket.IO
   replays any events still in the ring buffer.
3. The dedup set (`seen`) guarantees no double-applications.

---

## Pagination for huge sessions

For sessions with thousands of events, walk pages:

```ts
let since = 0;
const all = [];
do {
  const { data } = await api.get(
    `/api/apps/${appId}/sessions/${sessionId}/history?since_seq=${since}&events_limit=500`
  );
  all.push(...data.events);
  if (!data.events_has_more) break;
  since = data.events_next_seq;
} while (true);
```

Dedup inside each page by `ts` (globally unique across the whole
ledger), not `seq` (seq is per-counter and can overlap between
message and event kinds).

---

## Multimodal — images and attachments

User messages may carry images and files. Two formats the daemon
accepts (normalise on receive):

```jsonc
// Anthropic-native
{ "type": "image", "source": {
    "type": "base64", "media_type": "image/png", "data": "iVBORw0..." } }

// OpenAI-style
{ "type": "image_url", "image_url": { "url": "data:image/png;base64,..." } }

// File / document
{ "type": "file", "name": "report.pdf", "source": {
    "media_type": "application/pdf", "data": "JVBERi0..." } }
```

When a user message's `content` is an array, render each part
appropriately. For server-stored images (>1 MB), use the ID endpoint:

```
GET /api/apps/{app_id}/sessions/{session_id}/images/{image_id}
    → raw image bytes, Cache-Control: public, max-age=86400
```

---

## Error UI contract (MUST HAVE)

For EVERY `error` event the server emits, you MUST render something
concrete — don't let errors vanish. The `category` drives the
component:

| category | Component | Retry button? | Blocks input? |
|---|---|---|---|
| billing | Banner (red) with billing link | no | yes |
| quota | Banner (amber) with countdown | yes (after `retry_after_seconds`) | yes until reset |
| auth | Modal → credential picker | no (relogin instead) | yes |
| rate_limit | Toast (auto-retry) | auto | brief |
| provider | Inline message under last bubble | yes | no |
| network | Toast with spinner (auto-retry 3x) | yes | no |
| timeout | Inline | yes | no |
| content_filter | Red banner "Content flagged" | no | no |
| approval | Modal (already triggered by `approval_request`) | — | yes |
| validation | Highlight fields from `payload.errors[]` | fix & retry | no |
| concurrency | Disable composer, show "turn in progress" hint | wait for `message_done` | yes |
| security | Red banner "Permission denied" | no | yes |
| storage | Full-screen error page | no | yes |
| tool | Mark the tool chip as failed | yes | no |
| cancelled | No banner — just mark bubble as interrupted | — | no |
| internal | Toast + "Report issue" button | yes | no |

---

## State machine — minimal checklist

A session bubble is in one of:

```
        user_message
            │
            ▼
      ┌──────────────┐
      │ user bubble  │─── if queued ───► queued (show position)
      └──────────────┘
            │ message_started
            ▼
   ┌─────────────────────┐
   │ assistant streaming │─── token* ───► content grows
   │                     │─── tool_start ─► add chip
   │                     │─── tool_call ──► complete chip
   │                     │─── thinking* ──► thought bubble
   └─────────────────────┘
            │
   ┌────────┴─────────────┐
   ▼                      ▼
message_done         message_cancelled / error
   │                      │
   ▼                      ▼
  done               interrupted / failed
```

Every bubble carries `correlation_id` — use it as the dedup key for
events arriving out of order.

---

## What you MUST NOT do

1. **Don't dedup by `seq`.** Use `event_id`. seq is re-used between
   the session-room and user-room fan-outs for approvals.
2. **Don't treat `token` events as authoritative.** A lost `token`
   won't reappear. `assistant_stream_snapshot` fires periodically
   with the full text so far — prefer it for reconnect.
3. **Don't ignore `interrupted: true`** on the session summary. That
   means the daemon crashed mid-turn. The UI should show a clear
   "this conversation was interrupted — the last assistant reply may
   be incomplete" marker.
4. **Don't assume `tool_calls` in `messages[]` === `tool_start`/`tool_call`
   event count.** Messages are the final state; events are the live
   trace. Use messages for rendering the settled history, events for
   live progress.
5. **Don't drop unknown event types.** Log them in a dev panel and
   keep the session functional — the server may add new types.
6. **Don't poll `/history` continuously.** Socket.IO is the live
   channel. Use `/history` for cold open + reconnect backfill only.
7. **Don't show raw Python tracebacks to the user.** Render
   `payload.error` (human message) + a "Details" toggle for
   `payload.detail`.

---

## Testing your integration

The daemon ships `tools/test_history_unified.py`,
`tools/test_history_live_full.py`,
`tools/test_history_stress_full.py`,
`tools/test_history_graceful_shutdown.py`,
`tools/test_error_events.py`, and
`tools/test_error_classifier_coverage.py` — 99/99 live assertions
against Ollama. Mirror the server's persistence contract on the
client: your UI must reconstruct the exact same timeline the
daemon persisted. Run those as fixtures, then snapshot-test your
renderer output against them.

## TL;DR

1. `GET /history` returns `{messages, events, session_metadata,
   pending_queue, memory/preview snapshots, pagination cursors}`.
2. Render `messages[]` as bubbles (already denormalised).
3. Reduce `events[]` through the big switch above to rebuild live
   state (streaming text, tool chips, thinking, approvals, errors).
4. Dedup by `event_id`. Order by `seq`.
5. Reconnect: fetch `?since_seq=<last>`, re-join Socket.IO, same
   reducer applies to backfill + live events.
6. Every `error` event has a `category` that drives UI — never show
   a generic toast if we already classified it.
7. `ts` is globally unique µs — use it for tie-breaks and cross-page
   dedup.
