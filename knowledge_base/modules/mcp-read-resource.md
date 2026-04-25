---
id: mcp-read-resource
title: "mcp.read_resource (McpReadResource)"
type: module-action
module: mcp
action: read_resource
fqn: mcp.read_resource
short_name: McpReadResource
keywords: [mcp, read_resource, mcpreadresource, resources, lire_ressource_mcp]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# mcp.read_resource (McpReadResource)

## Description
Read a resource from an MCP server

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string | ✓ | — | Server ID where the resource lives. |
| `uri` | string | ✓ | — | Resource URI to read. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [read_resource]
```

## Aliases
`lire_ressource_mcp`

## Safety
- Risk level: **low**
