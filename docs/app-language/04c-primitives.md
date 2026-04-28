---
id: 04c-primitives
---

# Execution Primitives

Execution primitives are **always-available capabilities** injected by the `context_builder` module. Unlike meta-tools (which help discover other tools) or module actions (which depend on loaded modules), primitives work with **any action from any module** - including modules added in the future.

They are visible to the LLM regardless of tool injection mode (direct or discovery) and tool count.

## Overview

| Primitive | Description |
|-----------|-------------|
| `run_parallel` | Execute multiple actions simultaneously |
| `background_run` | Launch any action as a background task |
| `background_status` | Check if a background task is running/completed/failed |
| `background_result` | Retrieve the result of a completed task |
| `background_cancel` | Cancel a running background task |
| `background_list` | List all background tasks |
| `background_wait` | Wait for a task to complete (with timeout) |
| `watch_start` | Start a persistent periodic watcher on any tool |
| `watch_stop` | Stop and remove a watcher |
| `watch_pause` | Pause a watcher (keeps history) |
| `watch_resume` | Resume a paused watcher |
| `watch_status` | Get watcher metrics, last result, and config |
| `watch_list` | List all watchers with their state |
| `watch_history` | Get last N check results from a watcher |
| `schedule_once` | Schedule a one-shot job at a specific time or delay |
| `schedule_cron` | Schedule a recurring job with a cron expression |
| `schedule_cancel` | Cancel a scheduled job |
| `schedule_list` | List all scheduled jobs |
| `schedule_status` | Get detailed status of a scheduled job |
| `remember` | "Remind me to X at Y" - semantic shortcut for scheduling |
| `send_notification` | Send a notification through an output channel (email, webhook, log, etc.) |

## Parallel Execution

### run_parallel

Execute multiple actions concurrently via `asyncio.gather()`. All actions run at the same time - the total duration equals the slowest action, not the sum.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `actions` | array | **Yes** | List of actions to execute (1-50) |
| `actions[].name` | string | **Yes** | Fully qualified tool name (`module.action`) |
| `actions[].params` | object | No | Parameters for the action |

**Example - read 3 files simultaneously:**

```json
{
  "name": "run_parallel",
  "arguments": {
    "actions": [
      { "name": "filesystem.read", "params": { "path": "/tmp/a.txt" } },
      { "name": "filesystem.read", "params": { "path": "/tmp/b.txt" } },
      { "name": "filesystem.read", "params": { "path": "/tmp/c.txt" } }
    ]
  }
}
```

**Response:**

```json
{
  "total": 3,
  "succeeded": 3,
  "failed": 0,
  "results": [
    { "index": 0, "name": "filesystem.read", "success": true, "data": "contents of a.txt..." },
    { "index": 1, "name": "filesystem.read", "success": true, "data": "contents of b.txt..." },
    { "index": 2, "name": "filesystem.read", "success": true, "data": "contents of c.txt..." }
  ]
}
```

**Key behaviors:**

- Actions are **independent** - a failure in one does not cancel the others
- Results are returned in the **same order** as the input actions
- Security policies (BLOCK/APPROVE/AUTO) are checked **before** execution
- If an action requires approval, the entire `run_parallel` call returns `requires_approval` for that action
- Per-module concurrency limits (semaphores) are respected

### Performance

```
Sequential (3 HTTP calls × 2s each):
  GET api1    2s
  GET api2    2s
  GET api3    2s
  Total: 6s

run_parallel (same 3 calls):
  GET api1    2s
  GET api2    2s
  GET api3    2s
  Total: 2s (3x faster)
```

True parallelism for I/O-bound operations (HTTP, filesystem, database, shell). All module actions are I/O-bound.

### Cross-Module Parallel Execution

`run_parallel` works across any combination of modules:

```json
{
  "actions": [
    { "name": "http.get", "params": { "url": "https://api.example.com/users" } },
    { "name": "filesystem.read", "params": { "path": "/tmp/config.json" } },
    { "name": "database.fetch_results", "params": { "connection_id": "db", "query": "SELECT count(*) FROM users" } }
  ]
}
```

All three run at the same time - HTTP request, file read, and database query in parallel.

## Background Tasks

### background_run

Launch **any** action as a background task. Returns immediately with a `task_id` for tracking.

> **Note**: Module-specific background actions (`http.download`, `shell.background_run`) also auto-notify on completion - the agent can use them directly. `background_run` is useful for wrapping actions that are normally blocking (like `http.get` or `shell.run`).

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | **Yes** | Fully qualified tool name (`module.action`) |
| `params` | object | No | Parameters for the action |

**Example - background download:**

```json
{
  "name": "background_run",
  "arguments": {
    "name": "http.download",
    "params": {
      "url": "https://example.com/large-file.zip",
      "destination": "/tmp/large-file.zip"
    }
  }
}
```

**Response:**

```json
{
  "task_id": "bg-a1b2c3d4",
  "tool_name": "http.download",
  "status": "running",
  "started_at": "2026-03-12T14:30:00Z"
}
```

### background_status

Check the status of a background task.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | **Yes** | Task ID from `background_run` |

**Response (running):**

```json
{
  "task_id": "bg-a1b2c3d4",
  "tool_name": "http.download",
  "status": "running",
  "elapsed_seconds": 12.5
}
```

**Response (completed):**

```json
{
  "task_id": "bg-a1b2c3d4",
  "tool_name": "http.download",
  "status": "completed",
  "elapsed_seconds": 45.2
}
```

### background_result

Retrieve the result of a completed background task.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | **Yes** | Task ID from `background_run` |

**Response (completed):**

```json
{
  "task_id": "bg-a1b2c3d4",
  "status": "completed",
  "result": { "success": true, "data": "..." }
}
```

**Response (still running):**

```json
{
  "task_id": "bg-a1b2c3d4",
  "status": "running",
  "note": "Task is still running. Use background_wait or check back later."
}
```

### background_cancel

Cancel a running background task.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | **Yes** | Task ID from `background_run` |

**Response:**

```json
{
  "task_id": "bg-a1b2c3d4",
  "cancelled": true
}
```

### background_list

List all background tasks (running and completed).

**Parameters:** None.

**Response:**

```json
{
  "tasks": [
    { "task_id": "bg-a1b2c3d4", "tool_name": "http.download", "status": "completed", "elapsed_seconds": 45.2 },
    { "task_id": "bg-e5f6g7h8", "tool_name": "shell.run", "status": "running", "elapsed_seconds": 3.1 }
  ],
  "total": 2,
  "running": 1,
  "completed": 1
}
```

### background_wait

Wait for a background task to complete, with a timeout.

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | **Yes** | Task ID from `background_run` |
| `timeout` | float | No | Max seconds to wait (default: 60, max: 3600) |

**Response (completed within timeout):**

```json
{
  "task_id": "bg-a1b2c3d4",
  "status": "completed",
  "result": { "success": true, "data": "..." }
}
```

**Response (timeout exceeded):**

```json
{
  "task_id": "bg-a1b2c3d4",
  "status": "running",
  "note": "Timeout reached. Task is still running."
}
```

## Auto-Notifications

When **any** background task completes or fails, the agent is **automatically notified** via a system message injected into the conversation. This works for:

- Universal primitives: `background_run`
- Module-specific background actions: `http.download`, `shell.background_run`

The agent does NOT need to poll with `background_status`.

### How it works

1. Agent calls `http.download(url=..., destination=...)` or `background_run(name="http.get", params={...})` -- gets a task ID back immediately
2. Agent **continues working** on other things (answers questions, makes other tool calls, etc.)
3. When the download finishes, a system message is **automatically injected** before the next LLM call:

```
[BACKGROUND TASK COMPLETED] task_id=bg-a1b2c3d4, tool=http.download, elapsed=45.2s
Result: {"success": true, "path": "/tmp/file.bin", "bytes": 104857600}
```

4. The agent sees this notification and can inform the user or take follow-up actions

### Notification format

**On success:**
```
[BACKGROUND TASK COMPLETED] task_id=bg-a1b2c3d4, tool=http.download, elapsed=45.2s
Result: {"success": true, "path": "/tmp/file.bin"}
```

**On failure:**
```
[BACKGROUND TASK FAILED] task_id=bg-a1b2c3d4, tool=http.download, elapsed=2.1s
Error: Connection refused
```

**On success with large result (> 2000 chars):**
```
[BACKGROUND TASK COMPLETED] task_id=bg-a1b2c3d4, tool=database.fetch_results, elapsed=12.5s
Result (truncated): [first 2000 chars of result]... (45000 chars total)
Use background_result(task_id="bg-a1b2c3d4") to get the full output.
```

### Key behaviors

- **During agent turns**: notifications are injected before each LLM call - the agent sees them immediately
- **While waiting for user input**: the conversation loop polls every 500ms for completed tasks. When one finishes, it **proactively triggers a new agent turn** - the LLM responds without requiring user input
- Multiple notifications can be batched if several tasks finish between polls
- Large results are truncated to 2000 chars to avoid bloating context - use `background_result` for full output
- The agent is instructed in the system prompt that it will be auto-notified, so it doesn't waste tokens polling

### Proactive delivery (conversation mode)

In conversation mode, the user doesn't need to send a message for the agent to receive a notification. The conversation loop:

1. Waits for user input in a background thread
2. Polls the notification queue every 500ms
3. When a task completes -- injects the notification -- triggers `agent_turn()`
4. The LLM processes the result and responds (e.g., "Your download is complete!")
5. The user sees the response immediately, then continues typing

```
User: "Download this file in the background"
Agent: "Launching background download... task_id=bg-abc123"

   [user is typing something else...]
   [5 seconds later, download finishes]

Agent: "Download complete! File saved to /tmp/file.zip (12 MB)"

you > _   ← user can keep typing
```

This works because the input thread is **never interrupted** - it keeps waiting for stdin while the notification handler runs the agent turn in parallel.

### Manual management still available

Even with auto-notifications, the manual tools remain useful:

| Tool | When to use |
|------|-------------|
| `background_status` | Check progress of a still-running task |
| `background_result` | Get full result when notification was truncated |
| `background_cancel` | Stop a task you no longer need |
| `background_list` | See all tasks at a glance |
| `background_wait` | Block until a specific task finishes (synchronous wait) |

## When to Use What

| Scenario | Use |
|----------|-----|
| Read 5 files at once | `run_parallel` |
| Call 3 APIs simultaneously | `run_parallel` |
| Download a large file while continuing to chat | `background_run` (auto-notified on completion) |
| Run a long shell command without blocking | `background_run` (auto-notified on completion) |
| Mix of HTTP + DB + filesystem in one go | `run_parallel` |
| Long build/migration | `background_run` (continue working, get notified when done) |

**Rule of thumb:**
- Actions that finish in < 30s -- `run_parallel` (batch them)
- Actions that may take minutes -- `background_run` (non-blocking, auto-notified)

## YAML Configuration

`run_parallel` and `background_*` primitives are **automatically available** to every agent in every app - no configuration needed.

**Watchers require explicit opt-in** via `execution.watchers: true`:

```yaml
modules:
  http: {}
  filesystem: {}

execution:
  mode: conversation
  watchers: true  # Enable watch_* primitives

# run_parallel and background_* are always available
# watch_* primitives only appear when watchers: true
```
When `watchers: false` (default), the agent has no awareness of watchers - the `watch_*` tools are not injected and the system prompt doesn't mention them. This keeps the context clean for apps that don't need monitoring.

The only other requirement is that the target module is loaded. For example, to watch an HTTP endpoint, the `http` module must be declared.

## Security

Primitives respect the same security policies as direct tool execution:

- **BLOCK** actions are rejected with an error
- **APPROVE** actions trigger the approval queue (the user is prompted)
- **AUTO** actions execute immediately

Example: if `filesystem.write` requires approval, calling it via `run_parallel` still requires approval - the primitive does not bypass security.

```yaml
capabilities:
  default_policy: auto
  approve:
    - module: filesystem
      actions: [write]
  deny:
    - module: shell
      actions: [run]
```
In this config:
- `run_parallel([filesystem.read, filesystem.read])` -- executes immediately
- `run_parallel([filesystem.read, filesystem.write])` -- write action returns `requires_approval`
- `background_run(name="shell.run", ...)` -- blocked (denied)

## Architecture

Primitives are implemented as `@action` methods on `ContextBuilderModule` - the same mechanism as meta-tools. This means:

- They auto-register in the action registry (zero config)
- They appear in `_build_meta_tools_schema()` (discovery mode)
- They are injected via `_build_primitive_tools_schema()` (direct mode)
- The system prompt includes a dedicated "EXECUTION PRIMITIVES" section

Internally:
- `run_parallel` uses `gather_actions()` from `executor.py` - the same infrastructure that powers module execution
- `background_run` wraps any `module.execute()` as an `asyncio.Task` with done-callbacks for result capture
- All background tasks are cancelled on app shutdown (`on_stop()`)

### Auto-notification pipeline

```text
ContextBuilderModule                    agent_loop.py                 conversation.py
                                    
background_run()                        _inject_bg_notifications()    _get_next_input()
   asyncio.create_task()                                               
        _bg_done_callback()                                             poll every 500ms
             _bg_notifications.put()                                       drain_bg_notifications()
                                                                                 notifications found!
                    > drain + inject as          
                                             system messages             
                    >
                                                                          _handle_bg_notifications()
                                                                               inject + agent_turn()
                                                                               display LLM response
```

Two delivery paths:

1. **During agent loop** (`agent_loop.py`): `_inject_bg_notifications()` drains the queue before each LLM call
2. **During input wait** (`conversation.py`): `_get_next_input()` polls every 500ms, triggers a full agent turn on notification

The input thread (blocking `input()` in `run_in_executor`) is **never cancelled** - it survives across notification cycles. This avoids the problem of multiple threads competing for stdin.

---

## Watchers (Persistent Monitoring)

Watchers are the **most powerful primitive** - they allow the agent to **persistently monitor data sources over time** without blocking the conversation and without consuming tokens on every check.

### The Problem

A background task is fire-and-forget: one execution, one notification. But many real-world scenarios require **continuous observation**:

- Monitor an API endpoint for health
- Track the progress of a long-running process
- Watch a database metric for threshold breaches
- Observe file changes over time
- Poll an external service for status updates

A naive approach (check every 30s) means 120 LLM calls/hour. At ~1000 tokens per call, that's **120K tokens/hour** for routine checks where nothing changed.

### The Solution: Smart Escalation

Watchers solve this with **escalation strategies** - the check runs silently in the background, and the LLM is **only notified when something interesting happens**.

| Strategy | When the LLM is notified | Token savings | Best for |
|----------|-------------------------|---------------|----------|
| `on_change` (default) | Result differs from previous | ~24x (5 changes vs 120 checks/h) | API monitoring, state tracking |
| `on_error` | Error occurs OR recovery from error | ~100x (near-zero in normal conditions) | Health checks, uptime monitoring |
| `on_threshold` | Expression evaluates to true | Variable | Metric thresholds, alerts |
| `summary` | After N checks accumulated | ~10x (batch_size=10) | Periodic reports |
| `always` | Every check (debug only) | None | Testing, debugging |

### watch_start

Start a persistent watcher on any tool. The watcher runs in the background and checks periodically.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | **Yes** | - | Fully qualified tool name (`module.action`) |
| `params` | object | No | `{}` | Parameters for each check invocation |
| `interval` | float | No | `30.0` | Seconds between checks (min: 5, max: 3600) |
| `label` | string | No | `""` | Human-readable description |
| `notify_when` | string | No | `"on_change"` | Escalation strategy |
| `notify_config` | object | No | `{}` | Extra config for the strategy |

**Example - monitor an HTTP endpoint:**

```json
{
  "name": "watch_start",
  "arguments": {
    "name": "http.get",
    "params": { "url": "https://api.example.com/health" },
    "interval": 30,
    "label": "API health check",
    "notify_when": "on_error"
  }
}
```

**Response:**

```json
{
  "watcher_id": "a1b2c3d4e5f6",
  "tool_name": "http.get",
  "label": "API health check",
  "status": "running",
  "interval": 30,
  "notify_when": "on_error",
  "hint": "Watcher 'API health check' started. Checking http.get every 30s. You'll be notified via 'on_error' strategy."
}
```

### Escalation Strategies in Detail

#### `on_change` (default)

Notifies the LLM when the result **differs** from the previous check. The first check always notifies (establishes baseline).

```json
{
  "notify_when": "on_change"
}
```

Use case: monitor a deployment status endpoint - get notified when the status changes from "deploying" to "live" or "failed".

#### `on_error`

Notifies only when:
- An **error** occurs (tool execution fails or returns an error)
- **Recovery** from error (tool was failing, now succeeds again)

Zero notifications while everything is OK.

```json
{
  "notify_when": "on_error"
}
```

Use case: health checks - only alert when something breaks.

#### `on_threshold`

Notifies when a **safe expression** evaluates to true against the result.

```json
{
  "notify_when": "on_threshold",
  "notify_config": {
    "expression": "result.status_code != 200"
  }
}
```

**Supported operators:** `==`, `!=`, `>`, `<`, `>=`, `<=`

**Supported paths:** dot-notation on the result dict (e.g., `result.count`, `result.data.status`)

**Supported values:** numbers, strings (quoted), `null`, `true`, `false`

**Examples:**
```
result.status_code != 200
result.count > 100
result.error != null
result.data.progress >= 100
result.status == "failed"
```

No `eval()` is used - expressions are parsed safely.

#### `summary`

Accumulates N check results, then sends them all at once as a batch.

```json
{
  "notify_when": "summary",
  "notify_config": {
    "batch_size": 10
  }
}
```

With `interval=30` and `batch_size=10`, the LLM gets a summary every 5 minutes instead of 120 individual notifications per hour.

#### `always` (debug only)

Notifies on every single check. Use only for testing and debugging - defeats the purpose of token savings.

### watch_stop

Stop and remove a watcher permanently. History is discarded.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `watcher_id` | string | **Yes** | ID from `watch_start` |

### watch_pause / watch_resume

Pause a running watcher - the timer continues but checks are skipped. History is preserved. Resume to restart checks.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `watcher_id` | string | **Yes** | ID from `watch_start` |

### watch_status

Get detailed watcher metrics: check count, notification count, last result, recent history, and full configuration.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `watcher_id` | string | **Yes** | ID from `watch_start` |

### watch_list

List all watchers with their state, check counts, and notification counts. Running watchers shown first.

**Parameters:** None.

### watch_history

Get the last N check results from a watcher's history ring buffer (max 100 entries stored).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `watcher_id` | string | **Yes** | - | ID from `watch_start` |
| `last_n` | int | No | `10` | Number of entries (1-100) |

### Watcher Notifications

When a watcher's condition triggers, a notification is pushed to the same queue as background tasks:

```
[WATCHER UPDATE] watcher_id=a1b2c3d4e5f6, label="API health check", tool=http.get
Check #42 (interval: 30s, 3 notification(s) so far, strategy: on_error)
Error: Connection refused
```

```
[WATCHER UPDATE] watcher_id=a1b2c3d4e5f6, label="API health check", tool=http.get
Check #45 (interval: 30s, 4 notification(s) so far, strategy: on_error)
Result: {"status_code": 200, "body": "OK"}   ← recovery notification
```

For `summary` strategy, the notification includes the full batch:

```
[WATCHER UPDATE] watcher_id=abc123, label="Load monitor", tool=shell.run
Check #30 (interval: 10s, 3 notification(s) so far, strategy: summary)
Summary (10 checks): [{"check": 21, "result": ...}, {"check": 22, ...}, ...]
```

### Non-Blocking Design

Watchers do **not** block the conversation:

```
User: "Monitor this API every 30 seconds and tell me if it goes down"
Agent: [calls watch_start] -- "Watcher started! I'll monitor the API and notify you
       if anything changes. You can keep chatting normally."

User: "Read me the README file"
Agent: [calls filesystem.read] -- "Here's the README: ..."

   [2 minutes later, API returns 500]

Agent: " The API health check detected an error - the endpoint returned
       HTTP 500. This happened at check #4. Want me to investigate?"

User: "Yes, check the logs"
Agent: [calls shell.run] -- ...
```

The conversation flows naturally while watchers run silently in the background.

### Security

- Watchers respect the same security policies as all primitives
- **BLOCK** tools cannot be watched
- **APPROVE** tools cannot be watched (watchers need to run unattended - requiring human approval every 30s would be impractical)
- **AUTO** tools work normally

### Watcher Persistence

Watchers are **persisted** in the KV backend and survive daemon restarts. When a watcher is created, its state is saved as a `PersistedWatcher`:

```text
KV keys:
  watcher:{app_id}:{watcher_id}  -- serialized PersistedWatcher
  __watcher_index__{app_id}      -- set of watcher_ids (per app)
```

On daemon restart, the bootstrap process calls `restore_watchers(app_id)` which:

1. Reads all `PersistedWatcher` entries for the app from KV
2. Recreates the runtime `Watcher` objects with their original config
3. Resumes only watchers that were in `running` state (paused watchers stay paused)
4. Check counts and notification counts are preserved

When no session is connected at the time a watcher fires, the notification is **buffered** in the KV store (`notif_buf:{app_id}`, max 100, TTL 24h). Buffered notifications are drained and delivered when the session reconnects.

### API Endpoints

6 REST endpoints for SDK clients:

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/{app_id}/watchers` | Create a watcher |
| `GET` | `/{app_id}/watchers` | List all watchers |
| `GET` | `/{app_id}/watchers/{wid}` | Get status + history |
| `DELETE` | `/{app_id}/watchers/{wid}` | Stop watcher |
| `POST` | `/{app_id}/watchers/{wid}/pause` | Pause watcher |
| `POST` | `/{app_id}/watchers/{wid}/resume` | Resume watcher |

**Example - create a watcher via API:**

```bash
curl -X POST http://localhost:8080/api/apps/my-app/watchers \
  -H 'Content-Type: application/json' \
  -d '{
    "tool": "http.get",
    "params": {"url": "https://api.example.com/health"},
    "interval": 30,
    "label": "API health",
    "notify_when": "on_error"
  }'
```

### Updated Decision Table (with Watchers)

| Scenario | Use |
|----------|-----|
| Read 5 files at once | `run_parallel` |
| Call 3 APIs simultaneously | `run_parallel` |
| Download a large file while chatting | `background_run` |
| Run a long shell command | `background_run` |
| Monitor an API for health | `watch_start` (notify_when=`on_error`) |
| Track deployment progress | `watch_start` (notify_when=`on_change`) |
| Alert when metric exceeds threshold | `watch_start` (notify_when=`on_threshold`) |
| Periodic status reports | `watch_start` (notify_when=`summary`) |
| Observe file changes over time | `watch_start` (notify_when=`on_change`) |

**Rule of thumb:**
- Actions that finish in < 30s -- `run_parallel` (batch them)
- Actions that take minutes -- `background_run` (one-shot, auto-notified)
- Continuous observation over time -- `watch_start` (persistent, smart notifications)
- Actions at a specific time or on a schedule -- `schedule_once` / `schedule_cron`

---

## Scheduler (Persistent Time-Based Jobs)

The scheduler allows agents to **plan actions in the future** - one-shot timers, recurring cron jobs, and "remember me" reminders. Jobs are **persisted** in the KV backend and survive daemon restarts.

> **Requires opt-in**: `execution.scheduler: true` in the app YAML. When disabled, the agent has no awareness of scheduler primitives.

```yaml
execution:
  mode: conversation
  scheduler: true    # Enable schedule_* and remember primitives
  watchers: true     # Can be combined with watchers
```
### How It Works

1. The agent calls `schedule_once` or `schedule_cron` (or `remember` as a shortcut)
2. A `ScheduledJob` is created and persisted in the KV backend
3. The `SchedulerService` tick loop (1s resolution) detects when a job is due
4. The job fires: executes a tool call, sends an LLM prompt, or pushes a notification
5. The result is routed to an **output channel** (LLM conversation by default, or webhook/log/custom)

Jobs persist across daemon restarts - the scheduler reloads all active jobs from KV on startup.

### schedule_once

Schedule a one-shot job at a specific time or after a delay.

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `when` | string | **Yes** | - | When to fire (see Time Expressions below) |
| `action_type` | string | No | `"notification"` | `"tool_call"`, `"llm_prompt"`, or `"notification"` |
| `tool_name` | string | Conditional | - | FQN for `tool_call` (e.g. `http.get`) |
| `tool_params` | object | No | `{}` | Parameters for the tool call |
| `prompt` | string | Conditional | - | Message for `llm_prompt` or `notification` |
| `label` | string | No | `""` | Human-readable description |
| `output_channel` | string | No | `"llm_notification"` | Target output channel |
| `output_config` | object | No | `{}` | Per-delivery config for the channel |

**Example - one-shot health check in 5 minutes:**

```json
{
  "name": "schedule_once",
  "arguments": {
    "when": "in 5m",
    "action_type": "tool_call",
    "tool_name": "http.get",
    "tool_params": { "url": "https://api.example.com/health" },
    "label": "Post-deploy health check",
    "output_channel": "slack_alerts"
  }
}
```

**Response:**

```json
{
  "job_id": "a1b2c3d4e5f6",
  "schedule_type": "once",
  "run_at": "2026-03-13T15:35:00Z",
  "action_type": "tool_call",
  "tool_name": "http.get",
  "label": "Post-deploy health check",
  "output_channel": "slack_alerts",
  "status": "active"
}
```

### schedule_cron

Schedule a recurring job with a cron expression (5-field standard cron).

**Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `cron` | string | **Yes** | - | Cron expression (5 fields: min hour dom mon dow) |
| `action_type` | string | No | `"notification"` | `"tool_call"`, `"llm_prompt"`, or `"notification"` |
| `tool_name` | string | Conditional | - | FQN for `tool_call` |
| `tool_params` | object | No | `{}` | Parameters for the tool call |
| `prompt` | string | Conditional | - | Message for `llm_prompt` or `notification` |
| `label` | string | No | `""` | Human-readable description |
| `max_runs` | int | No | `0` | Max executions (0 = unlimited) |
| `output_channel` | string | No | `"llm_notification"` | Target output channel |
| `output_config` | object | No | `{}` | Per-delivery config for the channel |

**Example - daily report at 9 AM:**

```json
{
  "name": "schedule_cron",
  "arguments": {
    "cron": "0 9 * * *",
    "action_type": "llm_prompt",
    "prompt": "Generate a summary of yesterday's API errors from the logs.",
    "label": "Daily error report"
  }
}
```

**Example - health check every 5 minutes, max 100 runs:**

```json
{
  "name": "schedule_cron",
  "arguments": {
    "cron": "*/5 * * * *",
    "action_type": "tool_call",
    "tool_name": "http.get",
    "tool_params": { "url": "https://api.example.com/health" },
    "label": "API health check",
    "max_runs": 100,
    "output_channel": "ops_webhook"
  }
}
```

### schedule_cancel

Cancel an active or paused job.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `job_id` | string | **Yes** | Job ID from `schedule_once` or `schedule_cron` |

### schedule_list

List all scheduled jobs, optionally filtered by status.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `status` | string | No | - | Filter: `"active"`, `"paused"`, `"completed"`, `"failed"` |

**Response:**

```json
{
  "jobs": [
    {
      "job_id": "a1b2c3d4e5f6",
      "schedule_type": "cron",
      "label": "Daily error report",
      "status": "active",
      "run_count": 12,
      "next_run_at": "2026-03-14T09:00:00Z"
    },
    {
      "job_id": "f6e5d4c3b2a1",
      "schedule_type": "once",
      "label": "Post-deploy check",
      "status": "completed",
      "run_count": 1,
      "last_run_at": "2026-03-13T15:35:00Z"
    }
  ],
  "total": 2,
  "active": 1,
  "completed": 1
}
```

### schedule_status

Get detailed status of a specific job.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `job_id` | string | **Yes** | Job ID |

### remember

Semantic shortcut for scheduling. The agent calls this when a user says things like "remind me to check the deployment tomorrow at 9am" or "remind me to check the logs in 2 hours".

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `what` | string | **Yes** | - | What to remember / what to do |
| `when` | string | **Yes** | - | When (natural language or cron) |
| `action` | string | No | `"notification"` | `"notification"` or `"llm_prompt"` |

**Example:**

```json
{
  "name": "remember",
  "arguments": {
    "what": "Check the deployment status and report to the team",
    "when": "tomorrow at 9am"
  }
}
```

Under the hood, `remember` creates a `ScheduledJob` with `action_type="notification"` (or `"llm_prompt"` if `action="llm_prompt"`). When the job fires, the message is injected into the agent's conversation as a system notification.

### Time Expressions

The scheduler uses a **deterministic parser** (no LLM) to interpret time expressions in both English and French:

| Expression | Parsed as | Example |
|------------|-----------|---------|
| `"in 5m"`, `"in 2h"`, `"dans 30 minutes"` | Relative delay -- `once` | `run_at = now + delta` |
| `"tomorrow at 9am"`, `"tomorrow at 9am" (FR)` | Absolute time -- `once` | `run_at = 2026-03-14T09:00:00Z` |
| `"every day at 9am"`, `"every Monday at 10am" (FR)` | Natural cron -- `cron` | `cron = "0 9 * * *"` |
| `"0 9 * * *"` | Raw cron -- `cron` | Passed directly |
| `"2026-03-14T09:00:00Z"` | ISO 8601 -- `once` | Parsed directly |

### Output Channel Integration

Every scheduled job has an `output_channel` field that determines where results are delivered:

```
schedule_once(when="in 5m", action_type="tool_call", tool_name="http.get",
              tool_params={...}, output_channel="slack_alerts")
```

- Default: `"llm_notification"` - result is pushed to the agent's conversation
- Named channel: routes to any channel defined in the `channels:` YAML block
- See [Output Channels](05-channels.md) for full documentation

If the target channel is unavailable, results fall back to `llm_notification`. If no session is connected, notifications are buffered in the KV store (max 100 per app, TTL 24h) and delivered when the session reconnects.

### Scheduler Persistence

Jobs and their state are persisted in the KV backend:

```text
KV keys:
  job:{app_id}:{job_id}         -- serialized ScheduledJob
  __job_index__{app_id}         -- set of job_ids (per app)
  __all_active_jobs__           -- set of app_id:job_id (global, for scheduler loop)
  notif_buf:{app_id}            -- list[dict] (FIFO, max 100, TTL 24h)
```

On daemon restart:

1. The `SchedulerService` reloads all active jobs from KV
2. Priority queue is rebuilt (`heapq` of `(next_run_at, job_id)`)
3. Missed jobs are executed immediately (catch-up)
4. Watchers are also restored from KV (see `PersistedWatcher`)

### Scheduler YAML Configuration

```yaml
execution:
  mode: conversation
  scheduler: true                   # Enable scheduler primitives
  watchers: true                    # Can be combined
  default_channel: slack_alerts     # Default output for jobs and watchers

channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{env.SLACK_WEBHOOK}}"

  audit:
    type: log
    config:
      logger_name: "digitorn.audit"
      level: INFO
      format: json
```
### Complete Decision Table (All Primitives)

| Scenario | Use |
|----------|-----|
| Read 5 files at once | `run_parallel` |
| Download a large file while chatting | `background_run` |
| Monitor an API continuously | `watch_start` |
| Run a health check in 5 minutes | `schedule_once` |
| Daily report at 9 AM | `schedule_cron` |
| "Remind me to check the deploy tomorrow" | `remember` |
| Recurring cleanup every Sunday at 3 AM | `schedule_cron` |
| Send results to Slack instead of LLM | Use `output_channel` param |

**Rule of thumb:**

- Immediate batch -- `run_parallel`
- One-shot async -- `background_run`
- Continuous monitoring -- `watch_start`
- Future one-shot -- `schedule_once` / `remember`
- Recurring -- `schedule_cron`
- Notify user externally -- `send_notification`

## Notifications

### send_notification

Send a notification through an output channel declared in the app YAML. Use this to proactively communicate with the user via external channels - email, webhook, Slack, log, etc.

Requires a `channels:` block in the app YAML. See [Output Channels](05-channels.md) for channel configuration.

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `channel` | string | yes | - | Channel instance name as declared in app YAML (e.g. `"email_alerts"`, `"slack_ops"`) |
| `message` | string | yes | - | Notification message body (plain text, max 10000 chars) |
| `title` | string | no | `""` | Subject line or title |
| `priority` | string | no | `"normal"` | `"low"`, `"normal"`, `"high"`, `"critical"` |
| `tags` | list | no | `[]` | Optional tags for filtering/routing |
| `structured_data` | dict | no | `{}` | Optional machine-readable JSON payload |
| `output_config` | dict | no | `{}` | Per-delivery config - required when targeting a specific recipient (e.g. `{"to_address": "user@example.com"}` for email) |

#### Example

```
Agent: send_notification(
  channel="email_alerts",
  message="Deployment completed successfully for v2.3.1",
  title="Deploy Complete",
  priority="high",
  output_config={"to_address": "ops@example.com"}
)
```

If the channel has a `user_resolver` configured, `output_config` can be omitted - the recipient is resolved automatically from the current session.

---

## Workspace Module (replaced the removed workbench)

The workbench system was removed in favor of the [workspace module](../modules/reference/workspace.md),
which provides 6 actions (WsWrite, WsRead, WsEdit, WsGlob, WsGrep, WsDelete) backed by
an in-memory virtual filesystem that streams live to the client via Socket.IO and optionally
mirrors to disk. See the [workspace module reference](../modules/reference/workspace.md) for
the complete API.

