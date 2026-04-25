---
id: mcp-health-check
title: "mcp.health_check (McpHealthCheck)"
type: module-action
module: mcp
action: health_check
fqn: mcp.health_check
short_name: McpHealthCheck
keywords: [mcp, health_check, mcphealthcheck, health, verifier_mcp, mcp_health]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# mcp.health_check (McpHealthCheck)

## Description
Health check one or all MCP servers

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string |  | — | Server ID to check. Omit for all servers. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [health_check]
```

## Aliases
`verifier_mcp`, `mcp_health`

## Safety
- Risk level: **low**
