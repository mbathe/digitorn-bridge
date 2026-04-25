---
id: mcp-list-tools
title: "mcp.list_tools (McpListTools)"
type: module-action
module: mcp
action: list_tools
fqn: mcp.list_tools
short_name: McpListTools
keywords: [mcp, list_tools, mcplisttools, introspection, tools, lister_outils_mcp]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# mcp.list_tools (McpListTools)

## Description
List all tools exposed by a specific MCP server

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string | ✓ | — | Server ID to list tools from. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [list_tools]
```

## Aliases
`lister_outils_mcp`

## Safety
- Risk level: **low**
