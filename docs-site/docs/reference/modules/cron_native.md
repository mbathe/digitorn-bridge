---
id: cron_native
title: cron_native Module
sidebar_label: cron_native
sidebar_position: 10
description: Three actions to schedule, cancel, and remind - one-shot, recurring, natural-language delays.
---

# cron_native

Schedule any tool to run later. Three actions: one for
scheduling, one for cancelling, one for self-reminders. Built
on `SchedulerService` with a KV-backed `JobStore`.

| Property | Value |
|----------|-------|
| Module id | `cron_native` |
| Version | `1.0.0` |
| Action count | 3 |
| Type | shared (jobs stamped with `app_id` for per-app isolation) |
| Pip deps | `croniter` |

## Design notes

- **One action covers every timing need** - one-shot, delayed,
  cron-recurring. Pick the `when` format that fits.
- **Tool-agnostic** - schedule any module action. The job
  calls `execute_tool(tool=..., args=...)` at fire time and
  delivers the result through the activation pipeline.
- **Natural-language delays** - `when: "in 5m" / "in 2h" /
  "in 1d" / "in 30s"` without timezone math.
- **Per-app isolation** - shared module but jobs are
  namespaced as `cron_<app_id>_<suffix>` so listing or
  cancelling never crosses app boundaries.

## The 3 actions

### `cron_native.schedule` - run any tool later

| Param | Type | Required | Description |
|-------|------|:--------:|-------------|
| `when` | string | yes | One of: `"in 5m"` (delay), `"2026-04-15T09:00:00Z"` (ISO 8601), `"0 9 * * *"` (cron 5-field). |
| `action` | string | yes | Tool FQN or short name (`filesystem.read`, `WebSearch`, ...). |
| `args` | dict | no | Parameters for the tool. |
| `description` | string | no | Short label shown in UI / history. |
| `job_id` | string | no | Custom id; auto-generated when omitted. |

```
schedule(when="in 5m", action="WebSearch", args={"query": "Digitorn news"})
schedule(when="2026-04-15T09:00:00Z", action="Bash", args={"command": "backup.sh"})
schedule(when="0 9 * * 1-5", action="channels.reply", args={"text": "Standup!"})
```

Returns `{job_id, next_run_at, recurring: bool}`.

### `cron_native.cancel_schedule` - cancel a job

```python
cancel_schedule(job_id="cron_my-app_abcd1234")
# returns: {cancelled: true}
```

Returns an error if the job is unknown.

### `cron_native.remind` - self-reminder

Shortcut for scheduling a message back to the agent.

| Param | Type | Required | Description |
|-------|------|:--------:|-------------|
| `when` | string | yes | Same formats as `schedule.when`. |
| `what` | string | yes | Reminder text. |

```
remind(when="in 10m", what="Check build logs")
```

At fire time the daemon delivers `what` as a system message to
the owning session.

## Cron expression - 5 fields

| Position | Field | Range |
|----------|--------------|----------------------|
| 1 | minute | `0-59` |
| 2 | hour | `0-23` |
| 3 | day of month | `1-31` |
| 4 | month | `1-12` |
| 5 | day of week | `0-6` (Sunday = `0`) |

- Step: `*/15` = every 15 units.
- Range: `1-5` = inclusive.
- List: `1,3,5` = union.
- Combine: `0 9 1,15 * *` = 9am on the 1st and 15th.

Delegates to `croniter` - any expression croniter accepts is
valid.

## Configuration

```yaml
tools:
  modules:
    cron_native:
      config:
        max_jobs_per_app: 500       # backpressure cap per app
        persist_job_results: true   # store last result in KV for inspection
        timezone: "UTC"             # default TZ for ISO without offset
```

Jobs are persisted to the `SchedulerService` KV backend
(top-level daemon config - defaults to SQLite at
`~/.digitorn/scheduler.db`, configurable via
`server.kv_backend` for Redis).

## Cross-references

- App-config block reference (`tools.modules.cron_native`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- Triggers (channel-driven cron via the channels module):
  [Channels → cron adapter](../../language/40-channels.md#cron---schedule-trigger-inbound)
- Background sessions (where scheduled jobs land for
  multi-user routing):
  [Background Sessions](../../language/38-background-sessions.md)
