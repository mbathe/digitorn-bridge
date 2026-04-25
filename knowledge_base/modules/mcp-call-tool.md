---
id: mcp-call-tool
title: "mcp.call_tool (McpCallTool)"
type: module-action
module: mcp
action: call_tool
fqn: mcp.call_tool
short_name: McpCallTool
keywords: [mcp, call_tool, mcpcalltool, tools, execution, appeler_outil_mcp, call_mcp_tool]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# mcp.call_tool (McpCallTool)

## Description
Call a tool on a specific MCP server

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string | ✓ | — | Server ID where the tool lives. |
| `tool_name` | string | ✓ | — | Name of the tool to call. |
| `arguments` | object |  | — | Arguments to pass to the tool. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [call_tool]
```

## Aliases
`appeler_outil_mcp`, `call_mcp_tool`

## Safety
- Risk level: **medium**
