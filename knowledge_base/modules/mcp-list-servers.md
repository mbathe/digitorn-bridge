---
id: mcp-list-servers
title: "mcp.list_servers (McpListServers)"
type: module-action
module: mcp
action: list_servers
fqn: mcp.list_servers
short_name: McpListServers
keywords: [mcp, list_servers, mcplistservers, introspection, lister_serveurs_mcp, list_mcp_servers]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# mcp.list_servers (McpListServers)

## Description
List all connected MCP servers and their status

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [list_servers]
```

## Aliases
`lister_serveurs_mcp`, `list_mcp_servers`

## Safety
- Risk level: **low**
