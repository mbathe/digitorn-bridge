---
id: context_builder-get-tool
title: "context_builder.get_tool (GetTool)"
type: module-action
module: context_builder
action: get_tool
fqn: context_builder.get_tool
short_name: GetTool
keywords: [context_builder, get_tool, gettool, discovery, schema, internal]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.get_tool (GetTool)

## Description
Get the full schema for a specific tool. Internal - SearchTools now returns schemas directly.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | - | Fully qualified tool name in 'module.action' format (e.g. 'database.fetch_results', 'filesystem.read_file'). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [get_tool]
```

## Safety
- Risk level: **low**
