---
id: database-relations
title: "database.relations (DbRelations)"
type: module-action
module: database
action: relations
fqn: database.relations
short_name: DbRelations
keywords: [database, relations, dbrelations, schema, fk, foreign_keys, joinable]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# database.relations (DbRelations)

## Description
Show foreign key relationships for a table. Reveals how tables are connected — essential for writing JOINs. Example: relations(table="orders")

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `table` | string | ✓ | — | Table name to inspect. |
| `connection_id` | string |  | `default` | Connection to use. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [relations]
```

## Aliases
`fk`, `foreign_keys`, `joinable`

## Safety
- Risk level: **low**
