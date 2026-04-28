---
id: mcp-list-resources
title: "mcp.list_resources (McpListResources)"
type: module-action
module: mcp
action: list_resources
fqn: mcp.list_resources
short_name: McpListResources
keywords: [mcp, list_resources, mcplistresources, resources, introspection, lister_ressources_mcp]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# mcp.list_resources (McpListResources)

## Description
List resources from an MCP server

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string | ✓ | - | Server ID to list resources from. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [list_resources]
```

## Aliases
`lister_ressources_mcp`

## Safety
- Risk level: **low**
