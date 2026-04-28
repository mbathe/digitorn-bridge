---
id: database-transaction
title: "database.transaction (DbTransaction)"
type: module-action
module: database
action: transaction
fqn: database.transaction
short_name: DbTransaction
keywords: [database, transaction, dbtransaction]
permissions: [database:write]
risk_level: medium
irreversible: false
require_approval: false
---

# database.transaction (DbTransaction)

## Description
Control an explicit database transaction. Use op='begin' to open, op='commit' to persist, op='rollback' to undo. All sql() and bulk_insert() calls on the same connection_id automatically run inside the open transaction.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `connection_id` | string |  | `default` | Connection to control. Use the same id you passed to sql(). |
| `op` | string | ✓ | - | Operation: 'begin' to open a transaction, 'commit' to persist changes, 'rollback' to undo all uncommitted changes. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: database
      actions: [transaction]
```

## Tool usage instructions
```
Open / commit / rollback an explicit database transaction.

## Workflow
1. transaction(connection_id='main', op='begin')
2. sql(...) / bulk_insert(...) - they all run INSIDE the transaction automatically
3a. transaction(connection_id='main', op='commit')   ← persist
3b. transaction(connection_id='main', op='rollback') ← undo everything

## Rules
- Only one open transaction per connection at a time
- Forgotten commits auto-rollback after the transaction timeout (default 300s)
- A failing sql() inside a transaction does NOT auto-rollback - you decide
- On disconnect or session end, an open transaction is rolled back automatically

## When to use
- Multi-step changes that must succeed or fail together (money transfer, cascading updates, schema migrations).
- Trial changes you want to verify before committing.
```

## Safety
- Required permissions: `database:write`
- Risk level: **medium**
