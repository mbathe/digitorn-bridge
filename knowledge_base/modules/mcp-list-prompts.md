---
id: mcp-list-prompts
title: "mcp.list_prompts (McpListPrompts)"
type: module-action
module: mcp
action: list_prompts
fqn: mcp.list_prompts
short_name: McpListPrompts
keywords: [mcp, list_prompts, mcplistprompts, prompts, introspection, lister_prompts_mcp]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# mcp.list_prompts (McpListPrompts)

## Description
List prompt templates from an MCP server

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string | ✓ | — | Server ID to list prompts from. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [list_prompts]
```

## Aliases
`lister_prompts_mcp`

## Safety
- Risk level: **low**
