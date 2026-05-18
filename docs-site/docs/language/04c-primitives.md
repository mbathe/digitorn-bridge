---
id: primitives
---

# Execution Primitives

The `context_builder` module exposes a small set of primitives for
**parallel execution**, **background tasks**, **persistent
monitoring**, and **time-based jobs**. They wrap any tool action and
work uniformly across modules.

Every primitive listed on this page maps to a real `@action` decorator
in the codebase; entries are cited with file + line.

## What's available, at a glance

| Primitive | Action | Source | Gated by |
|-----------|--------|--------|----------|
| Parallel execution | `run_parallel` | `context_builder/actions_meta.py` | always |
| Background launch (one action, 5 modes) | `background_run` | `context_builder/actions_background.py` | always |
| Persistent watcher start | `watch_start` | `context_builder/actions_watchers.py` | `runtime.watchers: true` |
| Watcher control | `watch_stop`, `watch_pause`, `watch_resume`, `watch_status`, `watch_list`, `watch_history` | `context_builder/actions_watchers.py` | `runtime.watchers: true` |
| Schedule a tool call | `schedule` | `cron_native/module.py` | `runtime.scheduler: true` |
| Cancel a scheduled job | `cancel_schedule` | `cron_native/module.py` | `runtime.scheduler: true` |
| Self-reminder | `remind` | `cron_native/module.py` | `runtime.scheduler: true` |
| Long-term memory write | `memory.remember` | `memory/module.py` | `tools.modules.memory` |
| Send via output channel | `tools.channels.send_message` | `channels/module.py` | `tools.modules.channels` + a configured channel |

## `runtime` flags that gate the primitives

Two flags on `RuntimeBlock` (`schema.py`) gate the
watcher / scheduler families. Both default to `false`.

```yaml
runtime:
  watchers: true       # enables watch_* primitives
  scheduler: true      # enables schedule / cancel_schedule / remind
                       # (REQUIRES watchers: true; schema.py)
```

A daemon-side scheduler service runs in the background; setting
these flags switches the relevant action handlers from "registered
but hidden" to "exposed in the agent's tool index".

## Parallel execution

### `run_parallel`

`actions_meta.py`. Runs N actions concurrently via
`asyncio.gather`. Each action is independent - failures in one do
not cancel the others. Results come back in the same order as input.

**Params** (`RunParallelParams`, `params.py`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `actions` | list[ParallelAction] (1-50) | *required* | The list of actions to run. |

`ParallelAction` is a small inline record `{ name: <module.action>, params: <dict> }`.

```jsonc
// LLM-side call
{
  "name": "run_parallel",
  "arguments": {
    "actions": [
      { "name": "filesystem.read", "params": { "path": "README.md" } },
      { "name": "filesystem.read", "params": { "path": "CHANGELOG.md" } },
      { "name": "shell.bash",      "params": { "command": "pytest -x" } }
    ]
  }
}
```

The result is `{ results: [<r1>, <r2>, <r3>] }` in the same order.

### When to use it

- Multiple **independent** lookups (`filesystem.grep`, `web.fetch`,
  `database.sql`) that don't depend on each other's output.
- Bulk fan-out where one tool call per item is too sequential.

For tasks where order matters or each step needs the previous result,
chain them with `execute_tool` calls in successive turns instead.

## Background tasks - `background_run` (one action, five modes)

`actions_background.py`. **A single `@action`** with five modes
dispatched by params. The doc historically listed five separate
actions (`background_run`, `background_status`, `background_result`,
`background_cancel`, `background_list`, `background_wait`); none of
those exist as separate actions. There is exactly **one** action
named `background_run`.

**Params** (`BackgroundRunParams`, `params.py`):

| Field | Visibility | Default | Description |
|-------|-----------|---------|-------------|
| `name` | visible | `null` | Tool name (e.g. `database.sql`). Required for the launch mode. |
| `params` | visible | `{}` | Parameters for the launched tool. |
| `task_id` | hidden | `null` | Required for status / cancel / wait modes. |
| `cancel` | hidden | `false` | When true with `task_id`, cancels the task. |
| `wait` | hidden | `false` | When true with `task_id`, blocks until the task finishes. |
| `list_tasks` | hidden | `false` | When true, returns all background tasks. |
| `timeout` | hidden | `60.0` (1-3600) | Max seconds for the wait mode. |

The five modes are dispatched in `background_run`
(`actions_background.py`):

```
mode 1 - launch         params.name + params.params
mode 2 - status check   params.task_id
mode 3 - cancel         params.task_id + params.cancel=true
mode 4 - wait           params.task_id + params.wait=true [+ timeout]
mode 5 - list all       params.list_tasks=true
```

```jsonc
// Launch
{"name": "background_run",
 "arguments": {"name": "database.sql",
               "params": {"query": "VACUUM ANALYZE"}}}

// Status check
{"name": "background_run",
 "arguments": {"task_id": "a1b2c3d4e5f6"}}

// Cancel
{"name": "background_run",
 "arguments": {"task_id": "a1b2c3d4e5f6", "cancel": true}}

// Wait (blocking up to 120s)
{"name": "background_run",
 "arguments": {"task_id": "a1b2c3d4e5f6", "wait": true, "timeout": 120}}

// List all
{"name": "background_run",
 "arguments": {"list_tasks": true}}
```

Short alias in `tool_names.py` - `BackgroundRun` →
`context_builder.background_run`.

### Auto-notification

When a background task completes (success **or** failure), the
runtime injects a system message into the next agent turn:

```
[BACKGROUND TASK COMPLETED] task_id=a1b2c3d4 tool=database.sql elapsed=12.3s
```

The agent does **not** need to poll - it is notified automatically.
The notification carries enough info for the agent to call
`background_run(task_id=...)` again to fetch the full result.

### When to use it vs `Bash(run_in_background=true)`

- For **module actions** (database, web, custom modules):
  `background_run`.
- For **shell commands**: prefer
  `shell.bash(command=..., run_in_background=true)` directly. It has
  its own background-task table and process controls (kill, stream
  stdout, status). Same auto-notification.

## Watchers - persistent monitoring

`runtime.watchers: true` enables seven actions in
`context_builder/actions_watchers.py`. Watchers are a "set it and
forget it" primitive: you register a check, the daemon polls it on
its own schedule, and the agent is **only notified when something
interesting happens**.

### `watch_start`

`actions_watchers.py`. Starts a periodic check.

**Params** (`WatchStartParams`, `params.py`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string (1-256) | *required* | Tool to call on every check (FQN, `module.action`). |
| `params` | dict | `{}` | Params passed to the tool on every check. |
| `interval` | float [5, 3600] | `30.0` | Seconds between checks. |
| `label` | string ≤256 | `""` | Human-readable description. |
| `max_checks` | int [0, 10000] | `0` | Auto-stop after N checks. `0` = unlimited. `1` = one-shot timer / reminder. |
| `notify_when` | string | `"on_change"` | When to wake the agent. See below. |
| `notify_config` | dict | `{}` | Extra config for the `notify_when` strategy. |

`notify_when` strategies (`params.py`):

| Value | Wakes the agent when... | `notify_config` |
|-------|-------------------------|-----------------|
| `on_change` (default) | Result differs from the previous check. | (none) |
| `on_error` | The tool errors, or recovers after a streak of errors. | (none) |
| `on_threshold` | An expression evaluates to true on the result. | `{"expression": "result.status_code != 200"}` |
| `summary` | After every N checks, deliver a batched summary. | `{"batch_size": 10}` |
| `always` | Every single check. Debug only - kills the LLM token budget fast. | (none) |

### Other watcher actions

| Action | Source | Params |
|--------|--------|--------|
| `watch_stop` | `actions_watchers.py` | `{ watcher_id }` |
| `watch_pause` | `actions_watchers.py` | `{ watcher_id }` |
| `watch_resume` | `actions_watchers.py` | `{ watcher_id }` |
| `watch_status` | `actions_watchers.py` | `{ watcher_id }` |
| `watch_list` | `actions_watchers.py` | (none) |
| `watch_history` | `actions_watchers.py` | `{ watcher_id, last_n }` (last_n: 1-100, default 10) |

`WatcherIdParams` and `WatchHistoryParams` are at
`params.py`.

### Persistence and lifecycle

- Watchers survive across turns within a session (they are owned by
  the session, not the turn).
- A watcher does **not** survive a daemon restart unless the
  underlying app + session is restored. Stop watchers explicitly
  before tearing down a session (or rely on `max_checks` / a
  reasonable `interval`).
- Pausing leaves the watcher registered but skips checks; resuming
  picks back up at the next interval.

### Use cases

- Poll an external API for a status change (`on_change` /
  `on_threshold`).
- Detect drift in a file (`filesystem.read` + `on_change`).
- Re-run a database query and notify when row count crosses a
  threshold.
- Periodically summarize a fast-moving log into batched updates
  (`summary` strategy).

## Scheduler - `cron_native` (one-shot + recurring jobs)

`runtime.scheduler: true` exposes three `cron_native` actions to the
agent. The scheduler is daemon-backed
and survives daemon restarts via persisted state.

> **Important.** The scheduler block requires `runtime.watchers: true`
> as well (`schema.py`); the watcher loop is the heartbeat the
> scheduler runs on.

### `schedule`

`cron_native/module.py`. The single primitive that schedules
anything - one-shot or recurring.

**Params** (`ScheduleParams`, `cron_native/params.py`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `when` | string (1-200) | yes | See "Time formats" below. |
| `action` | string (3-128) | yes | FQN of the tool to invoke at fire time (e.g. `http.get`, `tools.channels.send_message`). |
| `args` | dict | no | Parameters passed to the target tool. Same keys as a direct call. |
| `name` | string ≤64 | no | Human-readable id. Reuse to overwrite an existing job. |
| `output_channel` | string ≤64 | no | Send the result to this channel instance instead of the default `llm_notification`. |
| `max_runs` | int ≥0 | no | Cap a recurring job at N runs. `0` = unlimited. Ignored for one-shot jobs. |

#### Time formats (`when`)

Three accepted shapes (`cron_native/params.py`):

1. **Relative one-shot**: `"in 5m"`, `"in 2h"`, `"in 1d"`, `"in 30s"`.
2. **ISO 8601 timestamp** (one-shot): `"2026-04-15T09:00:00Z"`. The
   `T` between date and time is required.
3. **Cron expression** (recurring, 5 fields,
   `minute hour day month weekday`):
   - `"0 9 * * *"` - every day at 9 a.m.
   - `"0 9 * * 1-5"` - weekdays at 9 a.m.
   - `"*/15 * * * *"` - every 15 minutes.
   - `"0 0 1 * *"` - first of each month at midnight.
   - Weekday: `0=Sun, 1=Mon, ..., 6=Sat`.

#### Examples

```jsonc
// One-shot: send a daily summary at 6 p.m. tomorrow
{"name": "schedule", "arguments": {
  "when": "in 1d",
  "action": "channels.send_message",
  "args": {"channel": "email_reports", "message": "Daily summary..."},
  "name": "tomorrow_summary"
}}

// Recurring: weekdays at 9 a.m., max 52 runs (one year)
{"name": "schedule", "arguments": {
  "when": "0 9 * * 1-5",
  "action": "http.get",
  "args": {"url": "https://api.internal(health probe)"},
  "name": "weekday_health_check",
  "max_runs": 52
}}
```

### `cancel_schedule`

`cron_native/module.py`.

**Params** (`CancelScheduleParams`, `cron_native/params.py`):
`{ job_id: string }`. The `job_id` is exactly what `schedule`
returned. Format is usually `cron_<app_id>_<name>` for named jobs or
`cron_<app_id>_<random_hex>` for unnamed ones.

### `remind`

`cron_native/module.py`. A self-prompt scheduled back into the
current session.

**Params** (`RemindParams`, `cron_native/params.py`):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `when` | string (1-200) | yes | Same three formats as `schedule` (relative / ISO / cron). |
| `message` | string (1-2000) | yes | Reminder text. Injected as a system message prefixed `[REMINDER from cron]`. |
| `name` | string ≤64 | no | Optional human id. Reuse to overwrite. |

The reminder fires by waking the same session with the prefixed
message. Useful for "remind me to check X in 2 minutes" workflows.

```json
{"name": "remind", "arguments": {
  "when": "in 2m",
  "message": "Check whether the build passed and tell the user."
}}
```

### What `cron_native` does **not** expose

The previous version of this doc listed `schedule_once`,
`schedule_cron`, `schedule_cancel`, `schedule_list`, `schedule_status`
as separate actions. **None of those exist** in the code today.
The cron_native module has exactly three actions: `schedule`,
`cancel_schedule`, `remind`.

There is no LLM-callable `list_jobs` or `job_status` action. The
job catalog is owned by the daemon's scheduler service; agents
manage jobs by reusing `name` (overwrite) or by knowing their
`job_id`s (returned by the original `schedule` call).

## Long-term memory - `memory.remember`

`memory/module.py`. Stores a fact that **survives compaction**.
At runtime, recall happens through the agent's working-memory
recall layer, not via a separate tool.

`memory` is a regular module - declare it under `tools.modules`:

```yaml
tools:
  modules:
    memory:
      config: {}
        # see modules/reference/memory.md for available config knobs
```

The full surface of `memory` is just four actions
(`memory/module.py`): `task_create`,
`task_update`, `set_goal`, `remember`. Short aliases (from
`tool_names.py`): `TaskCreate`, `TaskUpdate`, `Remember`.

The legacy `set_plan`, `update_plan_step`, `add_todo`, `update_todo`,
`note`, `resolve_note`, `add_fact`, `recall`, `forget`,
`track_entity`, `add_relationship`, `checkpoint`, `cache_content`,
`get_snapshot`, `add_episode` action names referenced in older docs
**do not exist**.

See [Cognitive Memory](05-memory.md) for how the agent uses these
in practice.

## Output via channels - `tools.channels.send_message`

When the agent needs to push a notification through an external
channel (Slack, email, webhook, ...) it calls
`tools.channels.send_message` from the `channels` module.

```yaml
tools:
  modules:
    channels: {}      # the module reads its instance config from tools.channels
  channels:
    slack_alerts:
      type: slack
      config:
        webhook_url: "{{secret.SLACK_WEBHOOK}}"
    email_reports:
      type: email
      config:
        smtp_host: smtp.example.com
        from_addr: bot@example.com
```

Then from a turn:

```json
{"name": "channels.send_message",
 "arguments": {"channel": "slack_alerts",
               "message": "Build failed on main."}}
```

The full `channels` action set (`channels/module.py+`) :
`send_message`, `reply`, `broadcast`, `list_providers`,
`provider_status`, `pause_provider`, `resume_provider`,
`provider_history`, `stats`, `simulate_event`, plus internal
`test_send`. Documented in
[Channels (Bidirectional I/O)](40-channels.md).

> The `send_notification` action referenced in the previous
> version of this doc (and in some `prompt.py` builders) **does not
> exist** as an `@action` decorator. The `SendNotificationParams`
> Pydantic model in `context_builder/params.py` is currently
> unused. To send a notification, call `tools.channels.send_message`.

## Decision matrix - when to use what

| Goal | Use | Notes |
|------|-----|-------|
| Run N independent tools in one turn | `run_parallel` | All run concurrently, results in order. |
| Launch a long task without blocking the turn | `background_run` (mode launch) | You're auto-notified on completion. |
| Check the result of a launched task | `background_run` (mode status, with `task_id`) | Or call with `wait=true` to block. |
| Run a shell command in the background | `Bash(command=..., run_in_background=true)` | Native shell-side background, with `task_status` / `task_kill`. |
| Periodically poll something and only wake on changes | `watch_start(notify_when="on_change")` | Requires `runtime.watchers: true`. |
| One-shot delayed action | `schedule(when="in 5m", action=..., args=...)` or `watch_start(max_checks=1)` | Scheduler is more semantic; watcher works too. |
| Recurring action (cron) | `schedule(when="0 9 * * 1-5", action=..., args=...)` | Requires `runtime.scheduler: true`. |
| Remind your future self in 2 min | `remind(when="in 2m", message=...)` | Wakes the same session with `[REMINDER ...]`. |
| Push a Slack / email / webhook notification | `channels.send_message(channel=..., message=...)` | Channel must be declared in `tools.channels`. |
| Persist a fact across compaction | `memory.remember` (alias `Remember`) | `memory` module must be loaded. |

## Cross-references

- Tool injection algorithm: [Tools](04-tools.md#adaptive-tool-injection)
- Built-in tools index: [Built-in Tools](04b-builtin-tools.md)
- Channels surface: [Channels (Bidirectional I/O)](40-channels.md)
- Memory surface: [Cognitive Memory](05-memory.md)
- Background-run + auto-notification deep dive in code:
  `context_builder/actions_background.py`
- Watcher loop + `notify_when` strategies in code:
  `context_builder/actions_watchers.py`
- Scheduler service: `cron_native/module.py`
- Source of truth for short ↔ FQN mapping: `tool_names.py`
