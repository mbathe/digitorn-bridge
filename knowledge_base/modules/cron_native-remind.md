---
id: cron_native-remind
title: "cron_native.remind (CronNativeRemind)"
type: module-action
module: cron_native
action: remind
fqn: cron_native.remind
short_name: CronNativeRemind
keywords: [cron_native, remind, cronnativeremind, scheduler, cron, reminder]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# cron_native.remind (CronNativeRemind)

## Description
Schedule a self-reminder. When the time comes, the daemon wakes the SAME session you are in right now, reloads its full context (history, memory, goal), and injects your message as a system reminder so you can act on what you promised. The session resumes naturally even if the user had closed it in the meantime.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `when` | string | ✓ | — | REQUIRED. When the reminder fires. Same THREE formats as `schedule()`: relative ('in 5m', 'in 2h', 'in 1d'), ISO 8601 ('2026-04-15T09:00:00Z'), or cron expression ('0 9 * * 1-5'). |
| `message` | string | ✓ | — | REQUIRED. The reminder text. When the cron fires, this is injected back into the same session as a system message prefixed with [REMINDER from cron]. Write it as a clear instruction to your future ... |
| `name` | string |  | `` | Optional human-readable name for the reminder job. If empty, a random id is generated. Reuse the same name to overwrite an existing reminder. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: cron_native
      actions: [remind]
```

## Tool usage instructions
```
# remind — schedule a self-reminder that wakes this session

Use this when YOU need to be reminded to do something later in the SAME conversation. The fired reminder reloads the full session (all messages, memory, goal, todos) and re-injects you as if you just received a system message — so you can pick up exactly where you left off and execute what you committed to.

## When to use this vs schedule()

- Use `remind` when YOU (the agent) need to come back to a
  task in the same conversation. The session is woken with
  full context.
- Use `schedule` when you need to fire a SPECIFIC TOOL CALL
  (e.g. an HTTP request, a shell command) at a future time,
  with no need to resume the conversation.
- Use `memory.remember` when you want to STORE A FACT that
  survives compaction but doesn't need to fire at a time.

## Format of `when`

Same three formats as `schedule()`:
  - Relative: 'in 5m', 'in 2h', 'in 1d', 'in 30s'
  - ISO 8601: '2026-04-15T09:00:00Z'
  - Cron: '0 9 * * 1-5' (recurring)

## Examples

### User asks you to ping them in 2 minutes
    remind(when='in 2m', message='Time to check the build status.')

### You started a long-running build, want to check on it later
    remind(when='in 10m',
           message='Check whether the npm build finished and report back.')

### Daily standup reminder for a recurring agent
    remind(when='0 9 * * 1-5',
           message='Pull the latest commits and summarize the team work.',
           name='daily_standup')

### Follow-up after a user request
    remind(when='in 1h',
           message='Follow up on the deployment and confirm it is healthy.')

## What happens at fire time

1. The daemon reloads your session (history + memory + goal)
2. Your `message` is injected as: '[REMINDER from cron] ...'
3. A new turn starts with you having full context
4. You see WHY you set the reminder and act on it

## Returns

{job_id, next_run, label}. Save job_id if you want to cancel
via cancel_schedule(job_id) before it fires.

## When NOT to use

- Don't use for very short delays (< 30s): just keep working.
- Don't use to fire a tool call without resuming conversation:
  use `schedule` instead.
- Don't use to store a passive fact: use `memory.remember`.
```

## Safety
- Risk level: **low**
