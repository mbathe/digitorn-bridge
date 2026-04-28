---
id: database-search-data
title: "database.search_data (DbSearch)"
type: module-action
module: database
action: search_data
fqn: database.search_data
short_name: DbSearch
keywords: [database, search_data, dbsearch, search, filter, chercher_donnees, find_data]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# database.search_data (DbSearch)

## Description
Search for data in a table by column value. Supports exact match and partial match (LIKE). Like Ctrl+F in a spreadsheet. Example: search_data(table="users", column="email", value="@gmail.com", mode="contains")

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `table` | string | ✓ | - | Table name to search. |
| `column` | string | ✓ | - | Column to search in. |
| `value` | string |  | `` | Value to search for. |
| `mode` | string |  | `contains` | Search mode: exact, contains, starts_with, ends_with. |
| `limit` | integer |  | `20` | Max results to return. |
| `connection_id` | string |  | `default` | Connection to use. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [search_data]
```

## Aliases
`chercher_donnees`, `find_data`, `filter`

## Safety
- Risk level: **low**
