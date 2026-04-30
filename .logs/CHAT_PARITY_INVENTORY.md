# Chat Parity Inventory — Daemon ↔ Flutter ↔ Web

Source of truth for the daemon's chat surface. For each feature we track:
**daemon ✓** (does the daemon actually emit/expose this?), **Flutter** (is
the desktop client wired to it?), **Web** (is the web client wired?), and
**Live verified** (we sat down and watched it work in both).

Status legend: `?` not investigated · `✓` works · `✗` missing · `~` partial

---

## 1. REST Endpoints (under `/api/apps/{app_id}`)

### Sessions lifecycle (`apps_v2/sessions.py`)

| Method | Path | Purpose | Flutter | Web | Live |
|---|---|---|---|---|---|
| POST   | `/sessions` | Create a session | ✓ | ✓ | ✓ |
| GET    | `/sessions` | List sessions | ✓ | ✓ | ? |
| GET    | `/sessions/search` | Search sessions | ? | ? | ? |
| GET    | `/sessions/{sid}` | Get session detail | ✓ | ✓ | ✓ |
| DELETE | `/sessions/{sid}` | Delete session | ✓ | ✓ | ? |
| POST   | `/sessions/{sid}/fork` | Fork from N-th turn | ? | ? | ? |
| POST   | `/sessions/{sid}/abort` | Cancel running turn | ✓ | ✓ | ✓ |
| POST   | `/sessions/{sid}/resume` | Resume after restart | ? | ? | ? |
| POST   | `/sessions/{sid}/undo` | Undo last user turn | ? | ? | ? |
| POST   | `/sessions/{sid}/compact` | Force context compaction | ? | ? | ? |
| GET    | `/sessions/{sid}/export` | Export transcript | ? | ? | ? |
| GET    | `/sessions/{sid}/history` | Replay messages | ✓ | ✓ | ? |
| GET    | `/sessions/{sid}/events` | Durable event log | ✓ (used in dev sniff) | ? | ✓ |
| GET    | `/sessions/{sid}/images/{id}` | Fetch attached image | ? | ? | ? |
| GET    | `/sessions/{sid}/memory` | Goals / facts / todos | ? | ? | ? |
| GET    | `/sessions/{sid}/preview` | Workspace preview snapshot | ? | ? | ? |
| GET    | `/sessions/{sid}/queue` | Pending message queue | ✓ | ✓ | ~ |
| DELETE | `/sessions/{sid}/queue/{eid}` | Remove queue entry | ? | ? | ? |
| POST   | `/sessions/{sid}/queue/clear` | Clear queue | ? | ? | ? |
| GET    | `/sessions/{sid}/active-ops` | Currently-running ops | ? | ? | ? |
| GET    | `/sessions/{sid}/context-breakdown` | Token usage by section | ? | ? | ? |
| GET    | `/sessions/{sid}/state` | Authoritative session state | ✓ | ✓ | ✓ |

### Messages (`apps_v2/messages.py`)

| Method | Path | Purpose | Flutter | Web | Live |
|---|---|---|---|---|---|
| POST | `/sessions/{sid}/messages` | Send user message | ? | ? | ? |

### Approvals (`apps_v2/approvals.py`)

| Method | Path | Purpose | Flutter | Web | Live |
|---|---|---|---|---|---|
| GET  | `/approvals` | List pending approvals | ? | ? | ? |
| POST | `/approve` | Resolve an approval | ? | ? | ? |

### Background tasks (`apps_v2/background.py`) — *Tier 2+*

22 endpoints (background tasks + background sessions + activations).
Out of scope for chat-tier-0; tracked separately when we get there.

---

## 2. Socket.IO Event Channel (`/events` namespace)

All events are pushed via `sio.emit("event", envelope, to=sid, namespace="/events")`.
Each envelope is a `SessionEvent` ([events/envelope.py](packages/digitorn/core/events/envelope.py))
with this shape:

```jsonc
{
  "type": "tool_call",          // fine-grained event type, see below
  "kind": "session",            // coarse category (auto-derived)
  "app_id": "...",
  "session_id": "...",
  "user_id": "...",
  "op_id": "...",               // groups events into a UI cycle
  "op_type": "turn|tool|agent|approval|compact|message|system",
  "op_state": "pending|running|waiting_approval|completed|failed|cancelled|timeout",
  "op_parent_id": "...",        // sub-agent → parent agent linkage
  "event_id": "evt_...",
  "ts": "2026-04-28T...",
  "seq": 42,                    // ordering, set by bus
  "correlation_id": "...",
  "payload": { /* type-specific */ }
}
```

### 2.1. Turn lifecycle (`op_type: turn`)

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `user_message` | User submitted a message | ✓ | ✓ | ✓ |
| `message_queued` | Message held while another turn is running | ✓ | ✓ | ~ (queue panel works; QUEUED badge fixed) |
| `message_merged` | Queued message merged into current turn | ✓ | ✓ | ? |
| `message_replaced` | Earlier draft replaced by new content | ✓ | ✓ | ? |
| `message_started` | Daemon began processing the turn | ✓ | ✓ | ✓ (race fix Step 6 + skeleton position fix) |
| `message_done` | Turn finished cleanly | ✓ | ✓ | ✓ (race fix: queue flip BEFORE emit) |
| `message_cancelled` | Turn aborted (user or system) | ✓ | ✓ | ~ |
| `queue_full` | Queue rejected the message | ✓ | ✓ | ? |
| `result` | Final assistant content for the turn | ✓ | ✓ | ✓ |
| `turn_complete` | Whole turn (all ops) wrapped up | ✓ | ✓ | ~ |
| `stream_done` | Streaming finished | ✓ | ✓ | ~ |
| `turn:heartbeat` | Liveness ping every ~3 s while running | ~ | ~ | ? |
| `abort` | Hard cancel | ✓ | ✓ | ✓ (used by credential cancel + queue refactor) |

### 2.2. LLM streaming (`op_type: turn`)

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `token` | Generic content delta | ✓ | ✓ | ✓ |
| `out_token` / `in_token` | Provider-typed token | ~ | ~ | ? |
| `assistant_stream_snapshot` | Full assistant message at flush points | ✓ | ✓ | ✓ (corrId fix daemon + defensive lookup web) |
| `thinking_started` | Reasoning began | ✓ | ✓ | ? |
| `thinking_delta` | Reasoning chunk | ✓ | ✓ | ? |
| `thinking` | Reasoning summary block | ✓ | ✓ | ? |
| `token_usage` | In/out token counts for the turn | ~ | ~ | ? |

### 2.3. Tools (`op_type: tool`)

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `tool_start` | Tool invocation begins | ? | ? | ? |
| `tool_call` | Tool finished (success or error in payload) | ? | ? | ? |
| `tool_end` | Cleanup / cycle close | ? | ? | ? |

### 2.4. Sub-agents (`op_type: agent`)

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `agent_spawn` | Sub-agent launched | ? | ? | ? |
| `agent_progress` | Sub-agent running update | ? | ? | ? |
| `agent_result` | Sub-agent finished | ? | ? | ? |
| `agent_cancel` | Sub-agent cancelled | ? | ? | ? |
| `agent_event` | Generic agent envelope (legacy / catch-all) | ? | ? | ? |

### 2.5. Approvals (`op_type: approval`)

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `approval_request` | Tool blocked, user must accept | ? | ? | ? |
| `approval_progress` | Long-running approval update | ? | ? | ? |
| `approval_resolved` | User accepted / rejected | ? | ? | ? |

### 2.6. Memory & TODOs

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `memory_update` | Goal / fact / todo changed | ? | ? | ? |

### 2.7. Compaction (`op_type: compact`)

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `compact_started` | Compaction begins | ? | ? | ? |
| `compact_done` | Compaction finished | ? | ? | ? |
| `compaction` | Durable snapshot of compacted state | ? | ? | ? |

### 2.8. Hooks

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `hook` | Generic hook fired | ? | ? | ? |
| `hook_notification` | Hook-driven user-visible notification | ? | ? | ? |

### 2.9. Workspace / Preview (live-app rendering)

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `preview:state_changed` | Preview KV state updated | ? | ? | ? |
| `preview:state_patched` | Partial KV update | ? | ? | ? |
| `preview:resource_set` | Resource added/replaced (file, node, …) | ? | ? | ? |
| `preview:resource_patched` | Partial resource update | ? | ? | ? |
| `preview:resource_deleted` | Resource removed | ? | ? | ? |
| `preview:resource_bulk_set` | Bulk replace channel | ? | ? | ? |
| `preview:channel_cleared` | Channel emptied | ? | ? | ? |
| `preview:cleared` | Whole preview cleared | ? | ? | ? |
| `preview:snapshot` | Full snapshot at session join | ? | ? | ? |
| `preview:delta` | Catch-up delta | ? | ? | ? |

### 2.10. Widgets (V1 dispatcher)

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `widget:render` | Mount/update widget | ? | ? | ? |
| `widget:update` | Widget state delta | ? | ? | ? |
| `widget:close` | Unmount | ? | ? | ? |
| `widget:error` | Render failure | ? | ? | ? |
| `widget:state` | State broadcast | ? | ? | ? |
| `widget:cleared` | Cleared by app | ? | ? | ? |
| `widget:snapshot` | Hydration | ? | ? | ? |

### 2.11. Background tasks & terminal output

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `bg_task_update` | Background task status changed | ? | ? | ? |
| `terminal_output` | Long-running shell stdout/stderr | ? | ? | ? |

### 2.12. Credentials prompt

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `credential_required` | Provider missing API key | ✓ | ✓ | ✓ (event-type promotion + retry pill on cancel/loop) |
| `credential_auth_required` | OAuth re-auth needed | ✓ | ✓ | ~ |

### 2.13. System

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `connected` | Socket.IO handshake done | ✓ | ✓ | ✓ |
| `status` | Daemon-level status update | ✓ | ✓ | ~ |
| `error` | Generic error envelope | ✓ | ✓ | ✓ (flags user bubble sendFailed + system marker) |
| `notification` | Background-trigger / cron user notification | ? | ? | ? |
| `notification_result` | Result of acting on a notification | ? | ? | ? |

### 2.14. Replay / hydration on `join_session`

| Event type | When | Flutter | Web | Live |
|---|---|---|---|---|
| `state:snapshot` | Authoritative session-state envelope | ? | ? | ? |
| `queue:snapshot` | Current queue contents | ? | ? | ? |

---

## 3. Tier proposal — current state

### Tier 0 — golden path (envoi → réponse streamée → tools → abort)

| Feature | Status | Notes |
|---|---|---|
| `POST /messages` + `POST /sessions` | ✓✓✓ | Verified live via dev sniffer + UI |
| `user_message` / `message_started` / `message_done` | ✓✓✓ | Full lifecycle, race fix applied |
| `token` streaming | ✓✓✓ | Both clients append correctly |
| `assistant_stream_snapshot` | ✓✓✓ | corrId fix + defensive lookup |
| `result` / `turn_complete` | ✓✓✓ | Terminal events emit AFTER queue flip |
| **`tool_start` / `tool_call` / `tool_end`** | **? ? ?** | **Not yet verified — next Tier 0 item** |
| `abort` + `/abort` | ✓✓✓ | Tested via credential cancel flow |
| Retry on send failure (5 paths) | ✓✓✓ | sendFailed + retry pill on both clients |

**Tier 0 remaining**: live-test the tool flow (call a tool from a message, verify both clients render the tool chip + result + post-tool tokens correctly).

### Tier 1 — robustness

| Feature | Status |
|---|---|
| Queue panel (queue, message_queued/merged/replaced) | ✓✓~ (queue refactor done daemon-side; UI tested manually but no live audit) |
| Attachments (images, files in `POST /messages`) | ? ? ? |
| Thinking blocks (`thinking_started/delta`) | ✓✓? (handled in code, no live audit) |
| `memory_update` (goals/todos/facts) | ? ? ? |
| `history` replay on session reopen | ✓✓? |
| `fork` / `undo` | ? ? ? |
| `context-breakdown` | ? ? ? |
| `turn:heartbeat` watchdog | ~ ~ ? |

### Tier 2 — advanced

| Feature | Status |
|---|---|
| Sub-agents (`agent_spawn/progress/result/cancel`) | ? ? ? |
| Approvals (`approval_request/progress/resolved`) | ? ? ? |
| Hooks (`hook` / `hook_notification`) | ? ? ? |
| Compaction (`compact_started/done`) | ? ? ? |
| Preview / Workspace (live-app rendering) | ? ? ? |
| Widgets (`widget:render/update/close`) | ? ? ? |
| Background tasks (`bg_task_update`, `terminal_output`) | ? ? ? |

---

## 4. Working agreement

- One feature at a time; explicit go-ahead before implementation.
- Diff minimal; no opportunistic refactors.
- For each feature: relire le code Flutter/web existant avant patch.
- Test live (daemon up + 2 clients) avant de cocher "Live".
- Pas de commit ni push sans demande explicite.

---

## 5. Recent work (2026-04-29 / 30)

- **Queue refactor (8 steps)**: unified `dispatch_turn` helper, race fix for "queued just after turn ends", physical delete of SqlQueueBackend, default Redis backend.
- **Snapshot duplicate bubble fix**: daemon emits `correlation_id` in `assistant_stream_snapshot`; web defensive lookup that never duplicates.
- **Typing skeleton position**: web no longer creates empty assistant bubble on `message_started`; skeleton renders at the exact position the next bubble will land.
- **Retry on send failure**: 5 failure paths (POST exception, daemon `error` event, watchdog 30 s, credential cancel, credential loop) all flag the user bubble with `sendFailed` + retry pill. Web AND Flutter, contract-strict 1:1.
- **Vertical spacing**: tightened user → assistant gap from 20-28 px to 8-12 px (iMessage style). Web AND Flutter aligned.

---

*Last updated: 2026-04-30 — Tier 0 mostly complete, only `tool_*` events left to verify live*
