---
id: mcp-get-prompt
title: "mcp.get_prompt (McpGetPrompt)"
type: module-action
module: mcp
action: get_prompt
fqn: mcp.get_prompt
short_name: McpGetPrompt
keywords: [mcp, get_prompt, mcpgetprompt, prompts, obtenir_prompt_mcp]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# mcp.get_prompt (McpGetPrompt)

## Description
Get a prompt template from an MCP server with arguments filled in

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `server_id` | string | ✓ | — | Server ID where the prompt lives. |
| `prompt_name` | string | ✓ | — | Name of the prompt template. |
| `arguments` | object |  | — | Arguments for the prompt template. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: mcp
      actions: [get_prompt]
```

## Aliases
`obtenir_prompt_mcp`

## Safety
- Risk level: **low**
