---
id: database-schema
title: "database.schema (DbSchema)"
type: module-action
module: database
action: schema
fqn: database.schema
short_name: DbSchema
keywords: [database, schema, dbschema, explore, db_schema, tables, structure]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# database.schema (DbSchema)

## Description
Explore database schema. Use what='tables' to list, what='describe' for one table in detail (columns, types, FK, indexes, sample rows), what='all' for the full dump.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `connection_id` | string |  | `default` | Connection to explore. Default: 'default'. Example: 'main' |
| `what` | string |  | `tables` | Scope: 'tables' (list all — start here), 'describe' (one table in detail), 'all' (full dump). |
| `table` | string |  | — | Table name — required when what='describe'. Example: 'users' |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [schema]
```

## Tool usage instructions
```
Explore the schema of a connected database.

## Modes
- schema(what='tables') — list all tables (START HERE for unfamiliar databases)
- schema(what='describe', table='users') — full detail on one table:
  columns + types + nullability + primary key + foreign keys + indexes + sample rows
- schema(what='all') — full schema dump for every table (use sparingly on large DBs)

## Workflow
1. schema(what='tables') → see what's available
2. schema(what='describe', table='<one>') → understand the table you'll query
3. relations(table='<one>') → see how it joins to other tables
4. sql(query='SELECT ...') → write the actual query
```

## Aliases
`db_schema`, `tables`, `structure`

## Safety
- Risk level: **low**
