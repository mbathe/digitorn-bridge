---
id: context_builder-search-tools
title: "context_builder.search_tools (SearchTools)"
type: module-action
module: context_builder
action: search_tools
fqn: context_builder.search_tools
short_name: SearchTools
keywords: [context_builder, search_tools, searchtools, discovery, search]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.search_tools (SearchTools)

## Description
Search for tools by keyword or description. Returns matching tools with full parameter schemas so you can call ExecuteTool directly.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `query` | string | ✓ | — | Natural-language description of what you want to do. Examples: 'read a file', 'execute SQL query', 'take screenshot'. |
| `max_results` | integer |  | `5` | Maximum number of results to return. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [search_tools]
```

## Tool usage instructions
```
Search for tools when you need a capability not in your current tool list.

## How to use
1. SearchTools(query='what you need') — find relevant tools
2. The result includes the full parameter schema for each tool
3. Call ExecuteTool(name='module.action', params={...}) to use it

## Tips
- Use natural language: 'send email', 'create chart', 'query database'
- Results are ranked by relevance
- The parameters field shows exactly what params to pass to ExecuteTool
```

## Safety
- Risk level: **low**
