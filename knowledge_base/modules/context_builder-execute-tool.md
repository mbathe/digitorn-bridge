---
id: context_builder-execute-tool
title: "context_builder.execute_tool (ExecuteTool)"
type: module-action
module: context_builder
action: execute_tool
fqn: context_builder.execute_tool
short_name: ExecuteTool
keywords: [context_builder, execute_tool, executetool, execution]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# context_builder.execute_tool (ExecuteTool)

## Description
Execute a tool by name. Use SearchTools first to find the tool and see its parameter schema.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | — | Fully qualified tool name in 'module.action' format (e.g. 'database.fetch_results'). |
| `params` | object |  | — | Parameters to pass to the tool. Must match the tool's schema. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [execute_tool]
```

## Tool usage instructions
```
Execute a discovered tool by its fully qualified name (module.action).

## Workflow
1. SearchTools('query') — find the tool and its parameter schema
2. ExecuteTool(name='module.action', params={...}) — execute it

## Important
- Use the exact name from SearchTools results (e.g. 'database.sql', not 'Sql')
- Pass params as a dict matching the schema from SearchTools
```

## Safety
- Risk level: **medium**
