---
id: cron_native
title: Cron Native Module
sidebar_label: cron_native
sidebar_position: 10
description: 3 actions to schedule, cancel, and remind — one-shot, recurring, natural-language delays.
---

# cron_native

Schedule any tool to run later — once or recurring. Built on the existing `SchedulerService` with a KV-backed `JobStore`.

**Three actions.** One for scheduling, one for cancelling, one for self-reminders.

| Property | Value |
|----------|-------|
| **Module ID** | `cron_native` |
| **Isolation** | shared; jobs are stamped with `app_id` so each app sees only its own |
| **Platforms** | All |
| **Dependencies** | `croniter` |

---

## Design Philosophy

- **One action covers every timing need** — one-shot, delayed, cron-recurring. Pick the `when` format that fits the user's intent.
- **Tool-agnostic** — schedule any module action. The scheduled job calls `execute_tool(tool=..., args=...)` at the fire time and delivers the result back through the activation pipeline.
- **Natural language delays** — `when: "in 5m"` / `"in 2h"` / `"in 1d"` / `"in 30s"` without timezone math.
- **Per-app isolation** — shared module but jobs are namespaced with `cron_<app_id>_<suffix>` so listing or cancelling never crosses app boundaries.

---

## Actions (3)

| Action | Risk | Purpose |
|--------|------|---------|
| `schedule` | medium | Schedule any tool to run later — one-shot or cron-recurring |
| `cancel_schedule` | low | Cancel a previously scheduled job by its `job_id` |
| `remind` | low | Schedule a self-reminder (natural-language `when`) |

---

### `schedule` — run any tool at a future time

**Visible params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `when` | string | yes | One of: `in 5m` / `in 2h` (delay), `2026-04-15T09:00:00Z` (ISO 8601), `0 9 * * *` (cron 5-field) |
| `action` | string | yes | Tool FQN or short name (`filesystem.read`, `WebSearch`, ...) |
| `args` | dict | no | Parameters for the tool. |
| `description` | string | no | Short label shown in UI / history. |
| `job_id` | string | no | Supply a custom ID; otherwise auto-generated. |

**Examples:**

```
schedule(when="in 5m", action="WebSearch", args={"query": "Digitorn news"}, description="5-min reminder")
schedule(when="2026-04-15T09:00:00Z", action="Bash", args={"command": "backup.sh"})
schedule(when="0 9 * * 1-5", action="channels.reply", args={"text": "Morning standup!"})
```

Returns `{job_id, next_run_at, recurring: bool}`.

### `cancel_schedule` — cancel a pending/recurring job

```
cancel_schedule(job_id="cron_my-app_abcd1234")
```

Returns `{cancelled: true}` or an error if the job is unknown.

### `remind` — schedule a self-reminder

A shortcut for scheduling a message back to the agent at a later time.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `when` | string | yes | Same formats as `schedule.when`. |
| `what` | string | yes | The reminder text. |

```
remind(when="in 10m", what="Check build logs")
```

At fire time the daemon delivers `what` as a system message to the owning session.

---

## Cron expression — 5-field

```
┌──── minute (0-59)
│ ┌── hour (0-23)
│ │ ┌ day of month (1-31)
│ │ │ ┌ month (1-12)
│ │ │ │ ┌ day of week (0-6, Sun=0)
│ │ │ │ │
* * * * *
```

- Step: `*/15` = every 15 units.
- Range: `1-5` = inclusive.
- List: `1,3,5` = union.
- Combine: `0 9 1,15 * *` = 9am on the 1st and 15th.

Delegates to `croniter` — any expression croniter accepts is valid.

---

## Configuration

```yaml
modules:
  cron_native:
    config:
      max_jobs_per_app: 500       # backpressure limit
      persist_job_results: true   # store last result in KV for inspection
      timezone: "UTC"             # default TZ for ISO without offset
```
Jobs are persisted to the `SchedulerService` KV backend (configured via the top-level daemon config, defaults to SQLite at `~/.digitorn/scheduler.db`).

---

## Removed / moved

Previous versions of this module documented 20+ actions (holidays, DAG deps, execution windows, `calendar_view`, `explain_cron`, etc.). None of those exist in current code. If you need that level of control, compose multiple `schedule` calls and pre-compute the next fire time in the agent.
