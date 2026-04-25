---
id: mcp-connect
title: "mcp.connect (McpConnect)"
type: module-action
module: mcp
action: connect
fqn: mcp.connect
short_name: McpConnect
keywords: [mcp, connect, mcpconnect, connection, lifecycle, connecter_mcp, connect_mcp_server]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# mcp.connect (McpConnect)

## Description
Connect to an MCP server (stdio subprocess, SSE, or HTTP)

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string | ✓ | — | Unique name for this server (e.g. 'slack', 'github'). Lowercase alphanumeric + underscores only. |
| `transport` | string |  | `stdio` | Transport type: 'stdio', 'sse', or 'streamable_http'. |
| `command` | string |  | — | Command to run (stdio only, e.g. 'npx', 'python'). |
| `args` | array |  | — | Command arguments (stdio only). |
| `env` | object |  | — | Environment variables for the subprocess (stdio only). |
| `url` | string |  | — | Server URL (sse/http only). |
| `headers` | object |  | — | HTTP headers (sse/http only). |
| `timeout` | number |  | `30.0` | Request timeout in seconds. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [connect]
```

## Aliases
`connecter_mcp`, `connect_mcp_server`

## Safety
- Risk level: **medium**
