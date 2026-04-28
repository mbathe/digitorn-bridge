---
id: cron_native-schedule
title: "cron_native.schedule (CronNativeSchedule)"
type: module-action
module: cron_native
action: schedule
fqn: cron_native.schedule
short_name: CronNativeSchedule
keywords: [cron_native, schedule, cronnativeschedule, scheduler, cron]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# cron_native.schedule (CronNativeSchedule)

## Description
Schedule any tool to run later - once or recurring. ONE action covers all scheduling needs. Pick the `when` format that fits: natural delay ('in 5m'), exact moment (ISO 8601), or recurrence (cron). The tool fires automatically at that time and the result is delivered back.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `when` | string | ✓ | - | REQUIRED. When to fire. THREE accepted formats: (1) RELATIVE delay one-shot - 'in 5m', 'in 2h', 'in 1d', 'in 30s'. (2) ISO 8601 timestamp one-shot - '2026-04-15T09:00:00Z' (must include the 'T' bet... |
| `action` | string | ✓ | - | REQUIRED. Fully qualified tool name to invoke when the job fires, in 'module.action' format. Examples: 'http.get', 'http.post', 'shell.bash', 'channels.send_message', 'rag.query', 'web.search', 'fi... |
| `args` | object |  | - | Parameters passed to the target tool when it fires. Same exact keys you would pass when calling the tool directly. Example: for action='http.get', use args={'url': 'https://...'}; for action='shell... |
| `name` | string |  | `` | Optional human-readable name for this job. If empty, a random id is generated. Reuse the SAME name to overwrite an existing job (useful when replacing a recurring schedule). Example: 'weekly_report... |
| `output_channel` | string |  | `` | Optional output channel where the tool's result will be delivered when the job fires. Common values: 'email', 'slack', 'webhook'. Leave empty (default) to deliver via the standard in-conversation l... |
| `max_runs` | integer |  | `0` | Cap a recurring (cron) job at this many runs total. 0 = unlimited (default). Ignored for one-shot jobs (ISO/'in X'). Example: max_runs=52 for a weekly job that runs for one year. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: cron_native
      actions: [schedule]
```

## Tool usage instructions
```
# schedule - fire any tool at a future time

Use this when the user asks for ANYTHING that should happen later, on a delay, or on a schedule. Reminders, recurring reports, follow-ups, retries, batch jobs, periodic checks - they all go through this single action.

## How to choose the `when` format

Pick the simplest format that matches the user's intent:

**1. Relative delay** - when the user says 'in N minutes/hours/days':
   `when='in 5m'`   → fires once in 5 minutes
   `when='in 2h'`   → fires once in 2 hours
   `when='in 1d'`   → fires once in 24 hours
   `when='in 30s'`  → fires once in 30 seconds

**2. ISO 8601 timestamp** - when the user gives an exact date/time:
   `when='2026-04-15T09:00:00Z'`        → April 15, 9am UTC
   `when='2026-12-25T18:30:00+01:00'`   → with timezone
   For natural language like 'tomorrow at 9am', YOU compute the
   ISO yourself before calling. Use the current date you know.

**3. Cron expression** - for recurring schedules. Format is the
   standard 5-field cron `minute hour day month weekday`:
   `when='0 9 * * *'`     → every day at 9:00
   `when='0 9 * * 1-5'`   → weekdays at 9:00
   `when='0 9 * * 1'`     → every Monday at 9:00
   `when='*/15 * * * *'`  → every 15 minutes
   `when='0 * * * *'`     → every hour on the hour
   `when='0 0 1 * *'`     → 1st day of every month at midnight
   `when='30 14 * * 0'`   → every Sunday at 14:30
   `when='0 9-17 * * 1-5'` → every hour 9am-5pm, weekdays
   Weekday: 0=Sun, 1=Mon, ..., 6=Sat. Range: `1-5`. Step: `*/15`.
   List: `1,3,5`. Combine: `0 9 1,15 * *` = 1st and 15th at 9am.

## The `action` parameter

The fully-qualified tool name in `module.action` format. Examples:
  - `'shell.bash'`              → run a shell command
  - `'http.get'` / `'http.post'` → call an API
  - `'channels.send_message'`   → deliver via email/slack/etc.
  - `'rag.query'`               → run a knowledge base query
  - `'web.search'`              → web search
  - `'filesystem.read'`         → read a file
Same module.action you would call directly without scheduling.

## The `args` parameter

The dict you would normally pass to that tool. Same exact keys.
Example: if `http.get` takes `{'url': '...'}`, then
`schedule(action='http.get', args={'url': '...'})` works the same.

## Full examples - copy these patterns

### Reminder in N minutes (most common case)
    schedule(
        when='in 10m',
        action='channels.send_message',
        args={'channel': 'llm_notification',
              'message': 'Time to check the build status.'}
    )

### Run a shell command in 1 hour
    schedule(
        when='in 1h',
        action='shell.bash',
        args={'command': 'pytest tests/ -v'}
    )

### Daily report at 9am every weekday
    schedule(
        when='0 9 * * 1-5',
        action='http.get',
        args={'url': 'https://api.example.com/daily-report'},
        name='weekday_report'
    )

### Weekly digest every Monday morning, capped at 52 runs
    schedule(
        when='0 8 * * 1',
        action='rag.query',
        args={'kb': 'inbox', 'query': 'summarize last week'},
        name='monday_digest',
        max_runs=52
    )

### One-shot at an exact ISO timestamp
    schedule(
        when='2026-12-25T09:00:00Z',
        action='channels.send_message',
        args={'channel': 'email', 'to': 'user@example.com',
              'subject': 'Merry Xmas', 'message': 'Happy holidays!'}
    )

### Health check every 5 minutes, deliver to slack
    schedule(
        when='*/5 * * * *',
        action='http.get',
        args={'url': 'https://my-api/health'},
        output_channel='slack',
        name='health_check'
    )

## Returns

On success: `{job_id, schedule_type, next_run, label, cron_expr?}`.
Always remember the `job_id` - you need it to cancel later.
If the same `name` is reused, the existing job is overwritten.

## Recurring vs one-shot

- Cron format → schedule_type='cron', fires every match
- ISO 8601 or 'in X' → schedule_type='once', fires exactly once
- Use `max_runs` to cap a cron schedule (0 = unlimited)

## When NOT to use schedule

- For something that should happen RIGHT NOW: just call the tool
  directly. Don't schedule(when='in 0s', ...).
- For 'remind me to keep this fact in mind during this conversation':
  use `memory.remember` instead - that's not a schedule.
- For very short delays (< 30s): consider whether you really need
  scheduling vs just continuing the work inline.

## Common mistakes

- Cron uses `*` for 'any value', not blank. `'9 * * *'` is INVALID
  (only 4 fields). Always 5 fields: minute hour dom month dow.
- 'Every weekday' = `1-5` (Mon-Fri), not `0-4` and not `MON-FRI`.
- 'Every hour at minute 0' = `0 * * * *`, not `* 0 * * *`.
- ISO timestamps must include the 'T' between date and time:
  `'2026-04-15T09:00:00Z'` ✓, `'2026-04-15 09:00:00'` ✗.
- The `args` dict keys must match the target tool's actual params.
  Read the target tool's signature first if unsure.
```

## Safety
- Risk level: **low**
