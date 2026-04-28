---
id: mcp-reconnect
title: "mcp.reconnect (McpReconnect)"
type: module-action
module: mcp
action: reconnect
fqn: mcp.reconnect
short_name: McpReconnect
keywords: [mcp, reconnect, mcpreconnect, connection, lifecycle, reconnecter_mcp]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# mcp.reconnect (McpReconnect)

## Description
Reconnect a failed MCP server

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string | ✓ | - | Server ID to reconnect. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [reconnect]
```

## Aliases
`reconnecter_mcp`

## Safety
- Risk level: **medium**
