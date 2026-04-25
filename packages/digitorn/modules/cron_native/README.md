# Cron Native Module

Enterprise cron scheduler — 7-field expressions, L/W/#/B modifiers,
timezones, holidays, DAG dependencies, retry with backoff, execution windows.

## Overview

The Cron Native module is a full-featured enterprise scheduler. It extends
standard 5-field cron with 7-field expressions (seconds + year), special
modifiers (L/W/#/B), timezone-aware scheduling, a holiday calendar, a DAG
for schedule dependencies, retry policies with exponential backoff, and
time-restricted execution windows.

It integrates with the existing `SchedulerService` (1-second tick loop) and
`JobStore` (KV-backed persistence). The module adds an intelligent layer
on top: dependency resolution, holiday calendars, execution windows, retry
logic, and a calendar view.

## Key Features

- **7-field cron** — `sec min hour day month weekday year`
- **Extended modifiers** — `L` (last day), `W` (nearest weekday), `#` (nth weekday), `B` (business days)
- **Timezone-aware** — IANA timezones with DST handling via `zoneinfo`
- **Holiday calendar** — one-time and recurring holidays, affects `B` expressions and window skips
- **DAG dependencies** — schedule B runs after A completes, with conditions (`success`/`failure`/`any`)
- **Cycle detection** — BFS reachability check prevents circular dependencies
- **Topological ordering** — Kahn's algorithm for correct execution order
- **Retry with backoff** — configurable max retries, initial delay, exponential multiplier, delay cap
- **Execution windows** — time-of-day + day-of-week + holiday skip restrictions
- **Calendar view** — preview all fire times across schedules in a date range
- **Human-readable explain** — `explain_cron("0 9 * * B")` → "at 09:00, on business days"

## Actions (21)

| Action | Description | Risk |
|--------|-------------|------|
| **Schedule CRUD** | | |
| `create_schedule` | Create a named cron schedule | Medium |
| `update_schedule` | Update schedule properties | Medium |
| `delete_schedule` | Delete schedule and clean up | Medium |
| `list_schedules` | List all schedules with status | Low |
| `schedule_info` | Detailed info with deps, retry, window | Low |
| **Cron Utilities** | | |
| `explain_cron` | Human-readable cron explanation | Low |
| `validate_cron` | Validate cron syntax | Low |
| `next_runs` | Compute next N fire times | Low |
| **Control** | | |
| `pause_schedule` | Pause a running schedule | Low |
| `resume_schedule` | Resume a paused schedule | Low |
| `run_now` | Trigger immediate execution | Medium |
| **History** | | |
| `execution_history` | Retrieve execution log | Low |
| **Dependencies (DAG)** | | |
| `add_dependency` | Add dependency with cycle detection | Medium |
| `remove_dependency` | Remove a dependency | Low |
| **Configuration** | | |
| `set_retry_policy` | Configure retry behavior | Low |
| `set_execution_window` | Restrict when execution is allowed | Low |
| **Holidays** | | |
| `add_holiday` | Add one-time or recurring holiday | Low |
| `remove_holiday` | Remove a holiday | Low |
| `list_holidays` | List all holidays | Low |
| **Bulk & View** | | |
| `bulk_create` | Create multiple schedules at once | Medium |
| `calendar_view` | Calendar preview of fire times | Low |

## Cron Expression Syntax

| Feature | Syntax | Example |
|---------|--------|---------|
| 5 fields | `min hour day month weekday` | `0 9 * * 1-5` |
| 7 fields | `sec min hour day month weekday year` | `0 0 9 * * 1-5 2026` |
| Last day | `L` | `0 0 L * *` |
| Nearest weekday | `W` | `0 0 15W * *` |
| Nth weekday | `#` | `0 0 * * 2#3` (3rd Tuesday) |
| Business days | `B` | `0 9 * * B` (Mon-Fri excl. holidays) |
| Ranges/Steps/Lists | `-` `/` `,` | `0 9-17/2 * * 1,3,5` |

## Architecture

```
CronNativeModule
    │
    ├── ExtendedCronExpression (cron_parser.py)
    │       ├── 5/6/7-field normalization
    │       ├── L/W/#/B post-processing
    │       ├── next_n() — compute fire times
    │       ├── explain() — human-readable
    │       └── validate() — syntax check
    │
    ├── ScheduleDAG (dag.py)
    │       ├── Cycle detection (BFS)
    │       ├── topological_order() (Kahn's)
    │       ├── get_ready(completed) → ready schedules
    │       └── Serializable (to_dict/from_dict)
    │
    ├── HolidayCalendar (calendar_utils.py)
    │       ├── One-time + recurring holidays
    │       ├── is_holiday() check
    │       └── as_date_set() for cron_parser
    │
    ├── SchedulerService integration
    │       ├── register_job() / unregister_job()
    │       └── JobStore persistence (KV-backed)
    │
    └── Execution Engine
            ├── Execution window check
            ├── Dependency satisfaction check
            ├── Retry with exponential backoff
            └── Dependent trigger (DAG.get_ready)
```

## Integration with SchedulerService

```
cron_native.create_schedule()
  → creates ScheduledJob in JobStore
  → registers via scheduler.register_job()
  → stores enriched metadata (deps, retry, window, holidays)

SchedulerService tick (1s)
  → fires the job
  → cron_native checks: window? holiday? dependencies?
  → if OK → execute (tool_call / llm_prompt / notification)
  → if retry needed → compute backoff → reschedule
  → on completion → DAG.get_ready() → trigger dependents
```

## App YAML Configuration

```yaml
modules:
  cron_native:
    config:
      default_timezone: Europe/Paris
      max_concurrent_jobs: 5
      history_limit: 100

    setup:
      - action: add_holiday
        params:
          date: "2026-12-25"
          name: Christmas
          recurring: true

      - action: create_schedule
        params:
          name: daily-report
          cron_expr: "0 9 * * B"
          timezone: Europe/Paris
          action_type: tool_call
          tool_name: database.fetch_results
          tool_params:
            connection_id: main
            query: "SELECT * FROM daily_summary"

      - action: set_retry_policy
        params:
          name: daily-report
          max_retries: 3
          retry_delay: 60
          backoff_multiplier: 2.0

      - action: set_execution_window
        params:
          name: daily-report
          start_time: "08:00"
          end_time: "18:00"
          days_of_week: [0, 1, 2, 3, 4]
          skip_holidays: true
```

## LLM Usage

```
1. cron_native.create_schedule       →  create "daily-report" schedule
2. cron_native.explain_cron          →  understand cron expression
3. cron_native.add_dependency        →  chain schedule B after A
4. cron_native.set_retry_policy      →  configure failure handling
5. cron_native.set_execution_window  →  restrict to business hours
6. cron_native.add_holiday           →  add company holidays
7. cron_native.calendar_view         →  preview all runs for next month
8. cron_native.execution_history     →  review past results
```
