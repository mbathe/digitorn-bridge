---
id: database-bulk-insert
title: "database.bulk_insert (DbBulkInsert)"
type: module-action
module: database
action: bulk_insert
fqn: database.bulk_insert
short_name: DbBulkInsert
keywords: [database, bulk_insert, dbbulkinsert, query, write, insert, bulk, inserer_lot, inserer_plusieurs, import]
permissions: [database:write]
risk_level: medium
irreversible: false
require_approval: false
---

# database.bulk_insert (DbBulkInsert)

## Description
Insert many rows into a table in one optimized call (batched, atomic). Use this instead of looping over sql() - much faster and cheaper in tokens for large inserts. Example: bulk_insert(table='users', columns=['name','email'], rows=[['Alice','a@x.com'],['Bob','b@x.com']]).

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `connection_id` | string | ✓ | - | Connection to insert into. |
| `table` | string | ✓ | - | Table name to insert into. |
| `columns` | array | ✓ | - | Column names in insertion order (e.g. ['name', 'email', 'age']). |
| `rows` | array | ✓ | - | List of rows to insert. Each row is a list of values matching the columns order. Example: [['Alice', 'alice@example.com', 30], ['Bob', 'bob@example.com', 25]]. For very large imports (>50k rows), c... |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [bulk_insert]
```

## Tool usage instructions
```
Insert many rows into a table efficiently.

## When to use
- Inserting more than ~10 rows: bulk_insert is much faster than calling sql() in a loop
- CSV / JSON ingestion
- Seeding test data

## Format
  bulk_insert(
    table='users',
    columns=['name', 'email', 'age'],
    rows=[
      ['Alice', 'alice@x.com', 30],
      ['Bob',   'bob@x.com',   25],
    ],
  )

## Notes
- Atomic: all rows succeed or all are rolled back
- Internally batched in chunks of 500 rows
- Inside an open transaction, runs in that transaction (no extra commit)
- Max 50 000 rows per call - split larger imports across multiple calls
```

## Aliases
`inserer_lot`, `inserer_plusieurs`, `import`

## Safety
- Required permissions: `database:write`
- Risk level: **medium**
