---
id: context_builder-background-run
title: "context_builder.background_run (BackgroundRun)"
type: module-action
module: context_builder
action: background_run
fqn: context_builder.background_run
short_name: BackgroundRun
keywords: [context_builder, background_run, backgroundrun, execution, background, primitive, arriere_plan, lancer_tache, bg_run, async_run]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# context_builder.background_run (BackgroundRun)

## Description
Run any tool in the background — returns task_id immediately.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string |  | — | Tool name to run in the background (e.g. 'database.sql'). |
| `params` | object |  | — | Parameters for the tool. |
| `task_id` | string |  | — | Task ID — for status/cancel/wait. |
| `cancel` | boolean |  | `False` | Cancel the task (requires task_id). |
| `wait` | boolean |  | `False` | Wait for completion (requires task_id). |
| `list_tasks` | boolean |  | `False` | List all background tasks. |
| `timeout` | number |  | `60.0` | Max seconds to wait (for wait mode). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [background_run]
```

## Tool usage instructions
```
Run a tool in the background. Returns a task_id immediately.
You are automatically notified when the task completes or fails.

## Modes
- BackgroundRun(name='database.sql', params={query: '...'}) → launch
- BackgroundRun(task_id='abc')                              → check status
- BackgroundRun(task_id='abc', cancel=true)                 → cancel
- BackgroundRun(task_id='abc', wait=true, timeout=120)      → block until done
- BackgroundRun(list_tasks=true)                            → list all tasks

## Auto-notification
When ANY background task completes or fails, you receive a system message:
  [BACKGROUND TASK COMPLETED] task_id=..., tool=..., elapsed=45s
You do NOT need to poll. Continue working and you will be notified.

## Note about shell commands
For shell commands, use Bash(command='...', run_in_background=true) directly.
```

## Aliases
`arriere_plan`, `lancer_tache`, `bg_run`, `async_run`

## Safety
- Risk level: **medium**
