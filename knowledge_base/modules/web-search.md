---
id: web-search
title: "web.search (WebSearch)"
type: module-action
module: web
action: search
fqn: web.search
short_name: WebSearch
keywords: [web, search, websearch]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# web.search (WebSearch)

## Description
Search the web for information.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `query` | string | ✓ | — | Search query. |
| `limit` | integer |  | `5` |  |
| `allowed_domains` | array |  | — |  |
| `blocked_domains` | array |  | — |  |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: web
      actions: [search]
```

## Tool usage instructions
```
Search the web. Returns results with title, URL, and snippet.

## When to use
- Finding documentation, error solutions, API references
- Getting current information not in your training data
- Researching libraries, frameworks, best practices
- Verifying facts or checking latest versions

## When NOT to use
- Information already in the codebase — Grep the project first
- Questions the user can answer — ask them instead
- Repeated searches for the same topic — remember results with Remember

## Workflow
1. Search('python asyncio timeout handling') → get URLs
2. Fetch(url) on the 2-3 most relevant results → get full content
3. Remember the key findings so they survive compaction

## Tips
- Be specific: 'python asyncio timeout error' not 'python error'
- Add version/year for current info: 'react 19 server components 2026'
- Results include title, URL, and snippet — scan snippets before fetching
```

## Safety
- Risk level: **low**
