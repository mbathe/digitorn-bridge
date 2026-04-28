---
id: database-browse
title: "database.browse (DbBrowse)"
type: module-action
module: database
action: browse
fqn: database.browse
short_name: DbBrowse
keywords: [database, browse, dbbrowse, explore, parcourir, navigate, scroll]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# database.browse (DbBrowse)

## Description
Browse a table interactively with pagination. Like scrolling through a spreadsheet. Shows rows with column names. Example: browse(table="users", page=1, per_page=20)

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `table` | string | ✓ | - | Table name to browse. |
| `page` | integer |  | `1` | Page number (1-indexed). |
| `per_page` | integer |  | `20` | Rows per page. |
| `connection_id` | string |  | `default` | Connection to use. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [browse]
```

## Aliases
`parcourir`, `navigate`, `scroll`

## Safety
- Risk level: **low**
