---
id: memory-task-create
title: "memory.task_create (TaskCreate)"
type: module-action
module: memory
action: task_create
fqn: memory.task_create
short_name: TaskCreate
keywords: [memory, task_create, taskcreate, todo]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# memory.task_create (TaskCreate)

## Description
Create a task to track your progress.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `subject` | string | ✓ | - | Brief title for the task. Example: 'Fix authentication bug'. |
| `description` | string |  | `` | What needs to be done. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: memory
      actions: [task_create]
```

## Tool usage instructions
```
Create a task to track progress. The user sees tasks in a dedicated panel.

## When to use
- Complex multi-step work (3+ steps) - create one task per step
- After receiving new instructions - break down requirements into tasks
- Before starting implementation - plan your work as tasks

## When NOT to use
- Single trivial operations - just do them directly
- Sub-agents should NEVER create tasks - the coordinator handles tracking

## Rules
- Create tasks BEFORE starting work, then update status as you go
- Keep subjects brief and actionable: 'Fix auth bug', 'Add input validation'
- One task per logical step - not one per file or one per line
- Update to in_progress before starting, completed when done
```

## Safety
- Risk level: **low**
