---
id: database-sql
title: "database.sql (DbQuery)"
type: module-action
module: database
action: sql
fqn: database.sql
short_name: DbQuery
keywords: [database, sql, dbquery, query, requete]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# database.sql (DbQuery)

## Description
Universal SQL execution. Auto-detects query type: SELECT/WITH/SHOW/EXPLAIN/PRAGMA return rows; INSERT/UPDATE/DELETE return affected row count; CREATE/ALTER/DROP return success.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `query` | string | ✓ | — | Any SQL query. SELECT returns rows, DML returns affected count. Always add LIMIT to SELECT. |
| `params` | array |  | — | Positional parameters for :p0, :p1, :p2 placeholders. Example: ['alice@example.com', 42] |
| `connection_id` | string |  | `default` | Connection to use. Default: 'default' |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [sql]
```

## Tool usage instructions
```
Execute any SQL query on a configured database connection.

## When to use sql() vs the other actions
- sql() is the default for any query you can write yourself
- Use schema() instead of sql('SELECT name FROM sqlite_master ...')
- Use bulk_insert() instead of sql() when inserting >50 rows — much faster
- Use browse() instead of sql() for paginated table previews
- Use search_data() instead of sql() for simple column lookups
- Wrap multi-step changes in transaction(op='begin')…transaction(op='commit')

## Query types
- SELECT / WITH / SHOW / EXPLAIN / PRAGMA → returns rows as a list of dicts
- INSERT / UPDATE / DELETE                → returns rows_affected
- CREATE / ALTER / DROP                   → returns success

## Parameters (NEVER interpolate user input)
  sql(query='SELECT * FROM users WHERE id = :p0', params=[42])
  sql(query='UPDATE users SET active=:p0 WHERE id=:p1', params=[False, 42])

## Tips
- ALWAYS add LIMIT to SELECT queries (default cap: 100 rows)
- Use sql('EXPLAIN <query>') to check the plan before slow queries
- Inside a transaction, sql() automatically runs in the open transaction
```

## Aliases
`query`, `requete`

## Safety
- Risk level: **medium**
