# cron_native - Integration Guide

`cron_native` gives the LLM **one primitive that schedules anything** and
**one primitive that cancels it**. No DAGs, holidays, retry policies, or
execution windows - those are decisions the agent makes in YAML or in
its response to a schedule firing.

## Actions

| Action | Purpose |
|---|---|
| `schedule` | Fire any tool call (once or recurring) at a future time. |
| `cancel_schedule` | Remove a previously registered job by id. |
| `remind` | Schedule a self-reminder that wakes the current session. |

## How it wires into your app

`cron_native` is **daemon-backed**: it talks to the scheduler service
already running inside the daemon (`SchedulerService` + `JobStore`). The
module itself carries no state across restarts - every job you register
is persisted in `~/.digitorn/digitorn.db` and replayed at boot.

```
agent → cron_native.schedule(...)
        │
        ▼
  ScheduledJob row in job_store
        │
        ▼
  SchedulerService.register_job()
        │
        ▼
  On fire → tool dispatch (same service_bus the agent uses)
```

## The `when` field - three formats

Parsed in order by `_parse_when`:

1. **Relative delay** - `"in 5m"`, `"in 2h"`, `"in 1d"`, `"in 30s"`
   Case-sensitive units (`s/m/h/d`). Bounded between `1s` and 10 years.
2. **ISO 8601 timestamp** - `"2026-04-15T09:00:00Z"` or with offset.
   Must be in the future (5 s grace for clock skew) and within 10 years.
3. **Cron expression** - exactly 5 fields (`minute hour dom month dow`)
   or an alias (`@daily`, `@hourly`). 6/7-field variants are rejected to
   keep per-second scheduling (a DoS vector) out of the surface.

## Constraints

The module ships no module-level constraints - all safety is at the
parser level (bounded delays, rejected past ISO, 5-field cron only).

## Isolation

`cron_native` runs as **shared** across sub-agents: every agent sees
the same scheduler instance, so a coordinator can cancel a job that a
specialist registered earlier in the same app. The module itself reads
`_app_id_override` to scope job ids per app (`cron_<app_id>_<name>`).

## When NOT to use

- For something that should happen **right now** → just call the tool.
- For passive facts the agent needs to remember → `memory.remember`.
- For fine-grained retries or back-off → let the caller loop, don't
  schedule rapid-fire retries (the 1 s minimum delay is deliberate).

## Related

- `packages/digitorn/core/app/scheduler.py` - `SchedulerService`
- `packages/digitorn/core/app/job_store.py` - persistence
- `docs/hooks.md` - hook events also fire through the same runtime
