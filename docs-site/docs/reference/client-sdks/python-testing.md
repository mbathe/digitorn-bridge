---
id: python-testing
title: Python Testing SDK
---

# Python Testing SDK (`digitorn.testing`)

The `digitorn.testing` package is the canonical client library for
writing live integration tests against a running Digitorn daemon.
It is shipped inside the `digitorn` PyPI package; no separate
install.

The SDK is a **library**, not a test runner. You write your own
scenarios under `tools/live_tests/<feature>_scenarios.py` (or any
other location) and import the SDK as a consumer would.

## Public surface

```python
from digitorn.testing import (
    DevClient,
    AppHandle,
    LiveEvent,
    LiveEventStream,
    SessionHandle,
    TurnResult,
    assertions,
)
```

| Symbol | Purpose |
|--------|---------|
| `DevClient` | Top-level client. Carries auth, talks REST + Socket.IO to the daemon. |
| `SessionHandle` | Lightweight value object identifying `(app_id, session_id, daemon_url, workspace)`. Constructed locally. |
| `AppHandle` | Same shape for apps. |
| `LiveEvent` | One Socket.IO event envelope as a `dataclass` with `seq`, `type`, `payload`. |
| `LiveEventStream` | A live tap on a session's Socket.IO stream. Buffer + helpers. |
| `TurnResult` | Final outcome of an agent turn. |
| `assertions` | Sub-module with `sort_by_seq`, `event_order`, ... helpers. |

## Connecting

```python
from digitorn.testing import DevClient

client = DevClient(
    daemon_url="http://127.0.0.1:8000",   # default
    auto_approve=True,                    # auto-approves every pending capability prompt
    timeout=30.0,                         # default request timeout
    token=None,                           # explicit JWT (else read from ~/.digitorn/auth.token)
)
```

The constructor reads the user's locally-stored auth token
(`~/.digitorn/auth.token` or equivalent) when `token=None`. For CI
or scripted environments, pass `token=` explicitly.

## Two flow shapes

### One-shot chat (REST polling)

```python
result = client.chat(app_id="my-app", message="Hello!")
print(result.content)
```

`chat` is the simple synchronous loop. Internally it creates a
session, POSTs the message, polls `GET /sessions/{sid}`, and
auto-approves pending prompts. Returns a `TurnResult` after
`message_done`.

Use this when you don't care about per-event hooks.

### Live event stream (Socket.IO)

```python
import uuid
from digitorn.testing import DevClient, SessionHandle

client = DevClient(daemon_url="http://127.0.0.1:8000", auto_approve=True)
session = SessionHandle(
    session_id=f"test-{uuid.uuid4().hex[:8]}",
    app_id="my-app",
    daemon_url="http://127.0.0.1:8000",
    workspace="",
)

stream = client.send_live(session, "ping", total_timeout=60.0)
try:
    events = stream.events()
    for ev in events:
        print(ev["type"], ev.get("payload", {}))
finally:
    stream.stop(timeout=2.0)
```

`send_live` POSTs the message AND opens a live Socket.IO stream
in one call. It waits for `message_done` (or `total_timeout`)
before returning the stream so `stream.events` is complete.

This is the path you want for per-event assertions (tool calls,
hooks fired, intermediate states).

## Manual control

When you need finer control (e.g. multiple POSTs on one stream):

```python
# Step 1 - POST without waiting
client.post_message_raw(session, "ping")

# Step 2 - wait for the daemon to register the session
client.wait_for_session(session, timeout=20.0)

# Step 3 - open the stream without waiting (already waited)
from digitorn.testing.events import LiveEventStream
stream = LiveEventStream(
    daemon_url=client.daemon_url,
    token=client._get_auth_token(),
    app_id=session.app_id,
    session_id=session.session_id,
)
stream.start()

# Step 4 - wait for an arbitrary event type
done = stream.wait_for("message_done", timeout=45.0)
```

## Assertions

```python
from digitorn.testing import assertions

events = assertions.sort_by_seq(stream.events())

ok, detail = assertions.event_order(
    events, ["user_message", "message_started", "tool_start", "tool_call", "message_done"]
)
assert ok, detail
```

`assertions.sort_by_seq` orders by the envelope's monotonic
`seq` field (Socket.IO does NOT guarantee delivery order across
namespaces, but `seq` does within a session).

`assertions.event_order` checks that the listed event types
appear in the given order. Returns `(ok, detail)`.

Other helpers worth knowing:

| Helper | Purpose |
|--------|---------|
| `assertions.contains_text` | The buffered `out_token` content concatenated includes a substring. |
| `assertions.tool_called` | A `tool_call` with the given name was emitted. |
| `assertions.no_errors` | No `error`-typed event landed. |

## Approvals

`auto_approve=True` (the default in tests) makes the client
poll `GET (apps API)` every second and resolve
every pending request. For tests of approval flows themselves,
pass `auto_approve=False` and call:

```python
pending = client.list_pending_approvals(app_id)
client.resolve_approval(app_id, request_id, approved=True)
```

## Auth

The client reads `~/.digitorn/auth.token` by default. You can
also pass an explicit `token=` to the constructor or set the
environment variable `DIGITORN_DEV_TOKEN`. In CI, mint a
short-lived token via the auth API and pass it explicitly.

## Where scenarios live

By convention, live test scenarios go under
`tools/live_tests/<feature>_scenarios.py` in the source repo.
Each scenario function returns a tuple
`(ok: bool, detail: str, artifacts: dict)`. The runner
in `tools/live_tests/run.py` drives them.

The `digitorn.testing` package itself contains **no scenarios**.
Adding a scenario inside this package is a violation of the
SDK's design - it is meant to stay a thin, reusable library.
