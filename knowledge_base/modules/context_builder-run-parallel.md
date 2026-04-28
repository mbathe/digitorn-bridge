---
id: context_builder-run-parallel
title: "context_builder.run_parallel (RunParallel)"
type: module-action
module: context_builder
action: run_parallel
fqn: context_builder.run_parallel
short_name: RunParallel
keywords: [context_builder, run_parallel, runparallel, execution, parallel, primitive, parallele, executer_parallele, batch, concurrent]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# context_builder.run_parallel (RunParallel)

## Description
Execute multiple tool calls in parallel.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `actions` | array | ✓ | - | List of actions to execute concurrently. Each runs independently; failures don't cancel others. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [run_parallel]
```

## Tool usage instructions
```
Run multiple independent tool calls concurrently - 3x to 10x faster than sequential.

## When to use
- Read multiple files at once
- Run multiple Grep searches at once
- Any independent operations that don't depend on each other

## Format
actions: [{name: 'filesystem.read', params: {file_path: 'a.py'}}, {name: 'filesystem.read', params: {file_path: 'b.py'}}]

## Important
- Results are returned in the same order as input actions
- Failures in one action do NOT cancel the others
```

## Aliases
`parallele`, `executer_parallele`, `batch`, `concurrent`

## Safety
- Risk level: **medium**
