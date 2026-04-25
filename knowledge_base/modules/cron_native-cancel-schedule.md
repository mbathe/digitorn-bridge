---
id: cron_native-cancel-schedule
title: "cron_native.cancel_schedule (CronNativeCancelSchedule)"
type: module-action
module: cron_native
action: cancel_schedule
fqn: cron_native.cancel_schedule
short_name: CronNativeCancelSchedule
keywords: [cron_native, cancel_schedule, cronnativecancelschedule, scheduler, cron]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# cron_native.cancel_schedule (CronNativeCancelSchedule)

## Description
Cancel a previously scheduled job. Pass the `job_id` returned by `schedule()`. After cancellation the job is removed from the scheduler and will never fire again.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `job_id` | string | ✓ | — | REQUIRED. The exact job_id returned by `schedule()` in the `job_id` field of its result. Format is usually 'cron_<app_id>_<name>' for named jobs or 'cron_<app_id>_<random_hex>' for unnamed ones. Do... |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: cron_native
      actions: [cancel_schedule]
```

## Tool usage instructions
```
# cancel_schedule — remove a scheduled job

Use this when the user asks to stop a scheduled job, change their mind about a reminder, or you need to replace a recurring job (cancel the old one, then `schedule()` a new one).

## Parameter

`job_id` — the exact value returned by `schedule()` in the
`job_id` field of its result. Format is usually
`cron_<app_id>_<name>` for named jobs, or
`cron_<app_id>_<random_hex>` for unnamed ones.

## Examples

### Cancel a job you just created
    result = schedule(when='in 1h', action='shell.bash',
                      args={'command': 'pytest'})
    # later, user changes their mind:
    cancel_schedule(job_id=result.data['job_id'])

### Cancel a named recurring job
    cancel_schedule(job_id='cron_myapp_weekly_report')

### Replace a recurring job (cancel + recreate)
    cancel_schedule(job_id='cron_myapp_daily_backup')
    schedule(when='0 3 * * *',  # new schedule
             action='shell.bash',
             args={'command': 'backup.sh'},
             name='daily_backup')

## Returns

On success: `{job_id, cancelled: True, ran_count}`.
`ran_count` tells you how many times the job had already fired
before being cancelled (useful for reporting to the user).

## When NOT to use

- Don't call this for a job that fired only once (one-shot jobs
  auto-complete and are removed by the scheduler — you don't
  need to cancel them).
- Don't call this with a guessed job_id. Only use ids you got
  back from a previous `schedule()` call in this same session
  or that the user explicitly gave you.
```

## Safety
- Risk level: **low**
