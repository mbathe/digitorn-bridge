# `digitorn.testing` - Live Testing SDK

**This package is the CLIENT LIBRARY used to write live tests against a running daemon. It is NOT a collection of tests.**

If you are an agent looking to test a feature, read this first. You almost certainly should NOT modify files in this directory.

---

## Layout

```
packages/digitorn/testing/    <-- THE LIB (this directory)
├── client.py                 DevClient: HTTP + REST helpers
├── events.py                 LiveEventStream: Socket.IO live tap
├── assertions.py             Primitives (seq_unique, event_order, sort_by_seq, report...)
├── models.py                 Dataclasses (SessionHandle, TurnResult, LiveEvent...)
└── __init__.py               Public exports

tools/live_tests/             <-- WHERE TESTS LIVE (outside the lib)
└── queue_scenarios.py        Example: queue + chronology scenarios
```

**Tests do NOT live in `packages/digitorn/testing/`.** They live outside and `import digitorn.testing` the same way any external consumer would.

---

## When to modify THIS package

Only when you need a **reusable primitive** that any future test will want.

- New endpoint not yet covered by `DevClient` → add a method to `client.py`.
- New event pattern worth asserting → add a helper to `assertions.py`.
- New event-stream capability (filtering, replay, multi-session join) → extend `events.py`.

Every added method must be **generic**. If the logic is specific to "the queue drain scenario" or "the abort test case", it does NOT belong here.

## When NOT to modify this package

- You want to test a specific feature (queue, abort, cross-session, MCP, RAG, preview snapshot, credentials flow, background, channels...). → Put your scenario in `tools/live_tests/<feature>_scenarios.py`.
- You want to run a one-off diagnostic. → Put a script in `tools/`.

If you find yourself adding scenario-specific code to `client.py`, stop. Move the generic part here and keep the scenario-specific code in your scenario file.

---

## Public API

```python
from digitorn.testing import (
    DevClient,         # HTTP + all REST helpers
    LiveEventStream,   # Socket.IO live tap
    SessionHandle,     # Session identifier + bag
    TurnResult,        # Structured turn outcome
    assertions,        # Primitives for writing checks
)
```

### `DevClient` - everything the daemon exposes

- Apps: `deploy`, `undeploy`, `get_app`, `list_apps`, `validate_yaml`
- Credentials: `set_secret`, `get_secrets`, `create_user_credential`, `list_user_credentials`, `delete_user_credential`
- Sessions: `create_session`, `send`, `chat`, `abort`, `abort_session(purge_queue=...)`, `resume`, `close_session`, `delete_session`, `fork_session`, `compact_session`, `export_session`, `list_sessions`, `wait_for_session`
- Messages / queue: `post_message_raw`, `get_queue(include_finished=...)`, `clear_queue`, `cancel_queue_entry`
- History & events: `get_history`, `get_events`, `get_persistent_events(since_seq=...)`, `get_context_breakdown`
- Workspace & preview: `get_workspace`, `get_workspace_file_content`, `get_preview_snapshot`, `get_code_snapshot`, `approve_workspace_file`, `reject_workspace_file`
- Memory & tools: `get_memory`, `get_tool_categories`, `search_tools`, `get_tool` (for memory todos created by `TaskCreate`, read `get_memory(session)["working"]["todos"]` — NOT `get_tasks`)
- Approvals: `get_pending`, `approve`, `deny`, `respond_to_ask`
- Background: `create_background_session`, `list_background_sessions`, `get_background_session`, `get_tasks` (background shell tasks for the session — long-running bash, NOT memory todos)
- Triggers: `get_triggers`, `fire_trigger`, `test_trigger`
- Daemon-level: `get_health`, `get_metrics`, `list_modules`, `list_mcp_servers`, `get_app_diagnostics`, `get_app_errors`, `get_activations`
- Live streaming: `open_event_stream(session)`, `send_live(session, message)`

### `LiveEventStream` - Socket.IO tap

Context-managed background thread that joins a session room, collects every envelope `(type, seq, kind, app_id, session_id, payload, ts)` into an in-memory log.

```python
with client.open_event_stream(session) as stream:
    # Live stream observes the assistant turn lifecycle. `user_message`
    # fires synchronously inside POST /messages BEFORE any stream can
    # subscribe (session is created lazily on first POST), so it is NOT
    # observable here. Read it from `get_persistent_events` after the turn.
    stream.wait_for("message_started", timeout=10)
    stream.wait_for(
        "message_done", timeout=60,
        predicate=lambda e: e["payload"]["correlation_id"] == cid,
    )
    events = stream.events()
```

### Events that are NOT observable on a live stream

These fire synchronously inside the POST handler before the live
stream can subscribe — same race as `user_message`:

- `user_message` (every turn).
- `slash_*` lifecycle on slash commands (`/help`, `/compact`, …).
  Use the HTTP response body of `post_message_raw(session, "/help")`:
  `data.status == "slash_handled"`, `data.command`, `data.correlation_id`.
- The first `behavior_directive` of turn 0 (fires before the first
  LLM call).

For these, assert via `get_persistent_events(session, since_seq=0)`
after the turn settles, or via the POST response. Don't try to catch
them with `stream.wait_for(...)` on a fresh session.

### `message_done` carries no text

`message_done.payload` only has `correlation_id`, `session_id`, and a
few flags (`slash_synthetic`, etc.). **It does not contain the
assistant's final text.** Two ways to get the text:

- Aggregate `out_token` (alias `token`) `payload.delta` chunks during
  the stream — this is what the Flutter / web clients do.
- After `message_done`, call `get_history(session)` and read the
  last `role: "assistant"` entry.

- `events()` - all envelopes (in arrival order, NOT seq order)
- `events_by_type(t)` - filter by type
- `last_seq()` - highest seq seen
- `wait_for(type, timeout, predicate=None)` - block until an event matches
- `wait_for_any(types, timeout)` - block until any of a set matches
- `wait_until_idle(quiet_seconds, total_timeout)` - block until stream is silent
- `clear()` - drop the local log (for long sessions)
- `stop(timeout)` - hard-stop the background thread; always call this or use `with`

All waits are guarded with strict timeouts. `atexit` hook also stops every stream on process exit - no hangs.

### `assertions` - checking primitives

- `seq_unique(events, exclude_types=None)` - every `seq` appears at most once
- `seq_is_monotonic` - alias of `seq_unique` (legacy name)
- `sort_by_seq(events)` - returns a new list sorted by `seq`
- `event_order(events, expected_types, strict_contiguous=False)` - expected types appear in this relative order
- `event_count(events, type, minimum, maximum=None)` - count of type is within bounds
- `correlation_id_thread(events, cid, expected_types)` - the events for a single correlation_id include the expected types
- `no_event(events, type)` - type must not appear at all
- `ephemeral_types_absent_from_persistent(persistent)` - high-volume streaming events must NOT end up in `session_events`
- `report(checks)` - aggregate `(name, (ok, detail))` tuples into a PASS/FAIL summary

**Rule for ordering checks**: always call `sort_by_seq(events)` before `event_order`. The wire order does NOT always match `seq` order (concurrent publishes can arrive reordered); the authoritative order is `seq`.

---

## How to write a new test scenario

1. Create a file outside this package, e.g. `tools/live_tests/my_feature_scenarios.py`.
2. Import from `digitorn.testing`:

   ```python
   import uuid
   from digitorn.testing import DevClient, assertions
   from digitorn.testing.models import SessionHandle

   def scenario_my_thing(client: DevClient, app_id: str) -> tuple[bool, str, dict]:
       sid = f"my-{uuid.uuid4().hex[:8]}"
       session = SessionHandle(
           session_id=sid, app_id=app_id,
           daemon_url=client.daemon_url, workspace="",
       )
       stream = None
       try:
           stream = client.send_live(session, "do the thing", total_timeout=60)
           events = assertions.sort_by_seq(stream.events())
           # `user_message` is persistent but NOT in the live stream of a
           # brand-new session (emitted before any subscriber). Read it
           # from get_persistent_events when you need to assert on it.
           persistent = client.get_persistent_events(session, since_seq=0)
           checks = [
               ("seq_unique", assertions.seq_unique(events)),
               ("live lifecycle", assertions.event_order(
                   events, ["message_started", "message_done"],
               )),
               ("user_message persisted", (
                   any((e.get("type") if isinstance(e, dict) else None) == "user_message"
                       for e in (persistent or [])),
                   "",
               )),
           ]
           ok, detail = assertions.report(checks)
           return ok, detail, {"session": sid, "event_count": len(events)}
       finally:
           if stream is not None:
               stream.stop(timeout=2.0)
   ```

3. The scenario returns `(ok: bool, detail: str, artifacts: dict)`. The runner aggregates across scenarios.

### Non-negotiables in scenario code

- **Always** wrap `LiveEventStream` use in `try / finally` with `stream.stop(timeout=2.0)`. Never leave a stream alive on exception - the `atexit` hook catches leaks, but explicit cleanup is faster.
- **Always** sort by `seq` before doing ordering assertions.
- **Never** add scenario-specific helpers to `digitorn.testing`. If a helper could serve two unrelated scenarios, it's generic and belongs in the lib; otherwise it stays local to your scenario file.
- **Never** depend on wire arrival order. Sort by `seq` first.

---

## Running scenarios

Pick your own runner. Example (direct function call):

```python
from tools.live_tests.queue_scenarios import scenario_single_turn
from digitorn.testing import DevClient

client = DevClient()
ok, detail, artifacts = scenario_single_turn(client, "digitorn-chat")
print("PASS" if ok else "FAIL"); print(detail)
```

Or via the thin runner at `tools/_run_scenario.py` (for quick CLI invocation):

```
py -3.12 tools/_run_scenario.py single_turn
```

---

## What a good test proves

A live test must prove behavior against the actual running daemon + a real LLM (not mocks). It validates:

1. **HTTP contract**: status codes, response shapes, correlation_ids.
2. **Event contract**: every expected event fires, in the right `seq` order, with the right payload fields.
3. **Durability**: persistent events survive - re-join with `since=0` replays identically.
4. **Isolation**: other sessions never see this one's events.
5. **Cleanup**: no stale queue rows, no running turns left hanging, no leaked streams.

If a scenario doesn't check those, it's telling you very little.

---

## Anti-patterns to refuse

- "Let's mock the LLM in `DevClient`." - No. This is a LIVE testing SDK. Mock at the app level if you must.
- "Let me add `test_queue()` to `client.py`." - No. That's a scenario, not a client capability.
- "I'll silently swallow the exception if the stream can't connect." - No. Live tests must fail loud; swallowing hides real bugs.
- "I'll poll the HTTP history in a loop." - Only as a fallback. Prefer `LiveEventStream.wait_for()` - it's the same signal the Flutter client sees.
