---
id: memory-set-goal
title: "memory.set_goal (MemorySetGoal)"
type: module-action
module: memory
action: set_goal
fqn: memory.set_goal
short_name: MemorySetGoal
keywords: [memory, set_goal, memorysetgoal, internal]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# memory.set_goal (MemorySetGoal)

## Description
Set the main goal for this session. Internal - use Remember for goals.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `goal` | string | ✓ | - | The goal to set. REQUIRED. Example: set_goal(goal="Fix the auth bug") |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: memory
      actions: [set_goal]
```

## Tool usage instructions
```
Set the main goal visible in memory at every turn. Use at the start of any non-trivial task.
```

## Safety
- Risk level: **low**
