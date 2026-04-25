---
id: mcp-disconnect
title: "mcp.disconnect (McpDisconnect)"
type: module-action
module: mcp
action: disconnect
fqn: mcp.disconnect
short_name: McpDisconnect
keywords: [mcp, disconnect, mcpdisconnect, connection, lifecycle, deconnecter_mcp, disconnect_mcp_server]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# mcp.disconnect (McpDisconnect)

## Description
Disconnect from an MCP server

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string | ✓ | — | Server ID to disconnect. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [disconnect]
```

## Aliases
`deconnecter_mcp`, `disconnect_mcp_server`

## Safety
- Risk level: **low**
