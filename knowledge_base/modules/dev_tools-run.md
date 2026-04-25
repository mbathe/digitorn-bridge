---
id: dev_tools-run
title: "dev_tools.run (DevToolsRun)"
type: module-action
module: dev_tools
action: run
fqn: dev_tools.run
short_name: DevToolsRun
keywords: [dev_tools, run, devtoolsrun, dev]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# dev_tools.run (DevToolsRun)

## Description
Run non-conversational apps — one-shot, pipeline, triggers, background, watchers.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `app_id` | string | ✓ | — | App ID. |
| `input_text` | string |  | `` | Input for one-shot apps. |
| `pipeline` | boolean |  | `False` | Run as pipeline (structured input). |
| `pipeline_input` | string |  | — | Pipeline structured input. |
| `trigger_id` | string |  | `` | Fire a trigger by ID. |
| `test_trigger` | boolean |  | `False` | Test-fire (dry run) instead of fire. |
| `trigger_payload` | object |  | — | Payload for fire_trigger. |
| `background_message` | string |  | `` | Create a background session with this message. |
| `background_payload` | object |  | — | Background session payload. |
| `list_bg_sessions` | boolean |  | `False` | List background sessions. |
| `bg_session_id` | string |  | `` | Inspect a specific background session. |
| `bg_pause_id` | string |  | `` | Pause a bg session. |
| `bg_resume_id` | string |  | `` | Resume a bg session. |
| `create_bg_task` | object |  | — | Create a background task (body). |
| `list_bg_tasks` | boolean |  | `False` | List background tasks. |
| `bg_task_id` | string |  | `` | Inspect / wait on a bg task. |
| `wait_bg_task` | boolean |  | `False` | Wait for bg task completion. |
| `cancel_bg_task_id` | string |  | `` | Cancel a bg task by id. |
| `list_triggers` | boolean |  | `False` | List app triggers. |
| `list_sessions` | boolean |  | `False` | List app sessions. |
| `list_watchers` | boolean |  | `False` | List active watchers. |
| `create_watcher` | object |  | — | Create a watcher (body). |
| `activations` | boolean |  | `False` | List activation history. |
| `errors` | boolean |  | `False` | List app errors. |
| `timeout` | number |  | `3600.0` |  |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: dev_tools
      actions: [run]
```

## Tool usage instructions
```
Non-conversational execution: one-shot, pipeline, triggers, background sessions, background tasks, watchers, activation history.

## One-shot (mode: one_shot)
  Run(app_id='research', input_text='Compare React vs Vue')

## Pipeline (mode: pipeline)
  Run(app_id='pipe', pipeline=true, pipeline_input={'urls': [...]})

## Triggers (mode: background)
  Run(app_id='bg', list_triggers=true)
  Run(app_id='bg', trigger_id='webhook', trigger_payload={...})
  Run(app_id='bg', test_trigger=true, trigger_id='cron')

## Background sessions (mode: background)
  Run(app_id='bg', background_message='hello', background_payload={...})
  Run(app_id='bg', list_bg_sessions=true) / bg_session_id=... / bg_pause_id=... / bg_resume_id=...

## Background tasks (long-running jobs)
  Run(app_id='x', create_bg_task={...}) / list_bg_tasks=true
  Run(app_id='x', bg_task_id='tid') / wait_bg_task=true
  Run(app_id='x', cancel_bg_task_id='tid')

## Watchers
  Run(app_id='x', list_watchers=true) / create_watcher={...}

## Activations / errors
  Run(app_id='x', activations=true) / errors=true

## When to use which
- Chat: mode: conversation apps (multi-turn, interactive)
- Run:  all other modes (one_shot, pipeline, background)
```

## Safety
- Risk level: **low**
