---
id: module-concept-cron_native
title: "cron_native module — overview"
type: module-concept
module: cron_native
isolation: shared
keywords: [cron_native, cron_native-module, schedule, cancel_schedule, remind]
version: 2.0.0
---

# `cron_native` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `2.0.0`
- **Actions**: 3 visible, 0 internal

## Description (from class docstring)

Cron native — single ``schedule()`` action backed by croniter + JobStore.

Design: 2 actions, no enterprise features (DAG, holidays, retry policies,
execution windows, calendar view). The whole point of this module is to
give the LLM ONE primitive that schedules anything, and ONE primitive
that cancels it. Everything else lives on the existing SchedulerService
+ JobStore infrastructure already wired in the daemon.

The ``when`` field accepts three formats, parsed in order:

1. **Cron expression** (5 fields): ``"0 9 * * 1-5"`` → recurring
2. **ISO 8601 timestamp**: ``"2026-04-15T09:00:00Z"`` → one-shot
3. **Relative offset**: ``"in 5m"`` / ``"in 2h"`` / ``"in 1d"`` → one-shot

For richer formats ("tomorrow at 9am") the LLM converts to ISO itself.

> Class-level summary: Two ultra-powerful actions: schedule + cancel_schedule.

## Configuration

Set under `modules.cron_native.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `schedule` | `CronNativeSchedule` |  | low | Schedule any tool to run later — once or recurring. ONE action covers all scheduling needs. Pick the `when` format th... |
| `cancel_schedule` | `CronNativeCancelSchedule` |  | low | Cancel a previously scheduled job. Pass the `job_id` returned by `schedule()`. After cancellation the job is removed ... |
| `remind` | `CronNativeRemind` |  | low | Schedule a self-reminder. When the time comes, the daemon wakes the SAME session you are in right now, reloads its fu... |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: cron_native
      actions: [schedule, cancel_schedule, remind]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {cron_native: [schedule, cancel_schedule, remind]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/cron_native-*.md`.
