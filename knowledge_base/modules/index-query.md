---
id: index-query
title: "index.query (IndexQuery)"
type: module-action
module: index
action: query
fqn: index.query
short_name: IndexQuery
keywords: [index, query, indexquery, search]
permissions: [index:read]
risk_level: low
irreversible: false
require_approval: false
---

# index.query (IndexQuery)

## Description
Search the index for entries matching a query. Searches across names, signatures, and summaries. Returns entries sorted by relevance.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `q` | string | ✓ | — | Search query — matches against entry names, signatures, and summaries. Examples: 'calculate_discount', 'users table', 'authentication'. |
| `kind` | string |  | — | Filter by entry kind: 'file', 'function', 'class', 'table', 'import', etc. |
| `source_id` | string |  | — | Filter results to a specific source. |
| `limit` | integer |  | `20` | Maximum number of results to return. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: index
      actions: [query]
```

## Safety
- Required permissions: `index:read`
- Risk level: **low**
