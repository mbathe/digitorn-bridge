# Digitorn API — Bruno collection

A curated set of ~35 requests covering the daemon's most-used routes. Every payload and field name was extracted from the actual Pydantic request models in `packages/digitorn/core/api/` — no guessing.

## Install

1. Open Bruno → **Open Collection** → pick this `digitorn-api/` folder.
2. Select the **Local** environment (top-right).
3. Edit `environments/Local.bru` — set `email` + `password` for your daemon.

## Intended run order

Variables chain automatically via `script:post-response`:

| Phase | Folder | What it captures |
|-------|--------|------------------|
| 0 | `00 - Health` | — |
| 1 | `01 - Auth` → **Login** | `access_token`, `refresh_token`, `user_id` |
| 2 | `03 - Discovery` | — |
| 3 | `04 - Credentials` | — |
| 4 | `05 - Apps` → **Deploy** | `test_app_id` |
| 5 | `06 - Sessions` → **Create session** | `session_id` |
| 6 | `07 - Messages` → **Send message** | — |
| 7 | `08 - Workspace` | — |
| 8 | `99 - Cleanup` → **Undeploy** | — |

All subsequent requests use `Authorization: Bearer {{access_token}}` automatically.

## Beyond the curated set

The daemon exposes **375 routes** in total. For the exhaustive list, run the **OpenAPI schema** request (`00 - Health/OpenAPI schema.bru`) — that returns the authoritative OpenAPI 3.x JSON which can be imported into Bruno's OpenAPI converter for 1:1 coverage.

## Socket.IO events (not in Bruno)

Bruno is HTTP-only — it can't do the Socket.IO handshake. For streamed events (tool_call, message_delta, agent_spawn, approval_request, etc.), open **`socket-io-tester.html`** in your browser instead.

What it does:
- Namespace `/events`, JWT auth via handshake (paste your `access_token` from Login)
- Click **Connect**, then **Join app room** or **Join session room** (needs app_id + session_id)
- Every server event is color-coded by kind (turn / tool / agent / approval / compact / system / error) with expandable JSON payloads and running counts

Event names observed in the code (from `core/events/envelope.py`):
- **turn**: `message_started`, `message_done`, `token`, `thinking_started`, `thinking_delta`, `assistant_stream_snapshot`
- **tool**: `tool_start`, `tool_call`, `tool_end`
- **agent**: `agent_spawn`, `agent_progress`, `agent_result`, `agent_cancel`
- **approval**: `approval_request`, `approval_resolved`, `approval_progress`
- **compact**: `compact_started`, `compact_done`
- **system**: `connected`, `status`, `error`, `abort`

Server emits on a single event name — `"event"` — with envelope `{type, kind, app_id, session_id, payload, ts, seq}`. Client emits `join_app` / `leave_app` / `join_session` / `leave_session` with ack callbacks.

**Typical flow to test a full turn:**
1. Login in Bruno (captures `access_token`)
2. Open `socket-io-tester.html` → paste base_url + token → Connect
3. Back in Bruno → Deploy → Create session (captures `session_id`)
4. In the HTML tester → paste app_id + session_id → **Join session room**
5. In Bruno → Send message
6. Watch the stream of `message_started` → `tool_start` → `tool_call` → `tool_end` → `message_done` in the HTML tester

## Common gotchas

- **`503 Retry-After: 2` on `/api/apps/{id}`** — daemon is still warming up after start. Poll `/health` until `warming_up: false`.
- **`401 Missing Authorization header` on `/docs`** — pre-patch daemon. Restart after the auth middleware fix.
- **`422` rejecting `audio` field on `/messages`** — POST your audio to `/api/transcribe` first, embed the returned text in `message`.
- **`Invalid character in header content`** on logout — client-side issue: your stored token has a trailing `\n`. Strip it before setting the Bruno env var.
