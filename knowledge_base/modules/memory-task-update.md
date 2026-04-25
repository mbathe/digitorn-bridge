---
id: memory-task-update
title: "memory.task_update (TaskUpdate)"
type: module-action
module: memory
action: task_update
fqn: memory.task_update
short_name: TaskUpdate
keywords: [memory, task_update, taskupdate, todo]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# memory.task_update (TaskUpdate)

## Description
Update a task's status.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `taskId` | string | ✓ | — | The task ID to update. Example: 't1'. |
| `status` | string | ✓ | — | New status: 'pending', 'in_progress', 'completed', 'blocked'. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: memory
      actions: [task_update]
```

## Tool usage instructions
```
Update task status in real-time as you work.

## Statuses
- pending: not started yet
- in_progress: currently working on it
- completed: fully done
- blocked: waiting on something

## Rules
- Mark as in_progress BEFORE starting work on a task
- Mark as completed IMMEDIATELY after finishing
- Only ONE task should be in_progress at a time
- Only mark completed when FULLY accomplished — not if tests fail
```

## Safety
- Risk level: **low**
