---
id: database
title: database Module
sidebar_label: database
sidebar_position: 8
description: Multi-driver async SQL - 16 actions, named connections, schema introspection, transactions, replicas, FK relations.
---

# database

Multi-driver async SQL module. Named connections, query
execution with safety LIMIT injection, full schema
introspection, transactions, bulk insert, paginated row
browsing, FK-aware relations, full-text search.

| Property | Value |
|----------|-------|
| Module id | `database` |
| Version | `1.0.0` |
| Type | user |
| Async drivers | `aiosqlite`, `asyncpg`, `aiomysql`, `aioodbc`, `oracledb` |

Short LLM names: `DbConnect`, `DbDisconnect`, `DbList`,
`DbQuery`, `DbTransaction`, `DbBulkInsert`, `DbSchema`,
`DbBrowse`, `DbRelations`, `DbSearch`.

## The 16 actions

 Grouped by responsibility.

### Connection management (3)

| Tool | Source | Purpose |
|------|--------|---------|
| `database.connect` | | Open a named connection. Params: `connection_id`, `driver`, `database`, `host`, `port`, `username`, `password_env`, `policy`, `role`, `group`, `persist`, `options`. |
| `database.disconnect` | | Close one connection (`connection_id="*"` closes all). |
| `database.list_connections` | | Active connections + metadata. |

### Query execution (4)

| Tool | Source | Purpose |
|------|--------|---------|
| `database.sql` | | **Recommended universal query** - auto-detects SELECT / INSERT / UPDATE / DELETE / DDL and injects a safety LIMIT on unbounded SELECTs. |
| `database.execute_query` | | Raw SQL execution (DDL / DML). Parameterised binding. |
| `database.fetch_results` | | SELECT with explicit LIMIT. |
| `database.transaction` | | Run a list of queries atomically (begin / commit / rollback managed). |

### Schema introspection (5)

| Tool | Source | Purpose |
|------|--------|---------|
| `database.schema` | | **Recommended unified explorer** - `what: "tables" \| "describe" \| "all"`. |
| `database.list_tables` | | List tables + columns + indexes. |
| `database.describe` | | Full table context - schema + sample rows. |
| `database.introspect` | | Full schema dump (every table). |
| `database.relations` | | FK graph - which tables reference which. |

### Data inspection (2)

| Tool | Source | Purpose |
|------|--------|---------|
| `database.browse` | | Paginated row browse for a table. |
| `database.search_data` | | Full-text / LIKE search across columns. |

### Bulk + indexing (2)

| Tool | Source | Purpose |
|------|--------|---------|
| `database.bulk_insert` | | Insert many rows in one call. |
| `database.extract_for_index` | | Extract schema in the shape the `index` module expects. |

## Recommended agent surface

For most apps, expose only the smart actions:

```yaml
tools:
  capabilities:
    grant:
      - {module: database, actions: [connect, sql, schema]}
```

`sql` auto-injects LIMIT on unbounded SELECTs and validates
syntax before executing. `schema` dispatches `tables` /
`describe` / `all` via its `what` param.

## Read-replica routing

Connections sharing a `group` with different `role` values
form a replica set:

```yaml
tools:
  modules:
    database:
      setup:
        - action: connect
          params:
            connection_id: main_db
            role: primary
            group: main
            driver: postgresql
            host: db.internal
        - action: connect
          params:
            connection_id: main_replica
            role: replica
            group: main
            driver: postgresql
            host: replica.internal
```

Read queries against the group are distributed across replicas
(round-robin).

## Per-connection security policies

Pass `policy` on `connect`:

| Preset | Allows |
|--------|--------|
| `readonly` | SELECT only. |
| `safe_write` | SELECT + INSERT / UPDATE, no DDL. |
| `unrestricted` | Everything. |

```yaml
- action: connect
  params:
    connection_id: main
    driver: postgresql
    database: prod
    host: db.internal
    password_env: DB_PASSWORD
    policy:
      preset: safe_write
      allowed_tables: [users, orders]
      blocked_tables: [credentials]
      blocked_keywords: [DROP, TRUNCATE, ALTER]
      max_rows: 10000
```

## Constraints

| Constraint | Type | Description |
|------------|------|-------------|
| `allowed_hosts` | `string_list` | Allowed DB hosts. By default only loopback. |
| `allowed_actions` | `string_list` | Restrict which DB actions are exposed. |
| `blocked_actions` | `string_list` | Block specific actions. |

```yaml
tools:
  modules:
    database:
      constraints:
        allowed_hosts: [localhost, db.internal]
        allowed_actions: [connect, sql, schema, browse]
```

## SQL injection safety

All query actions use parameterised binding - never
interpolate untrusted values into SQL strings. Use `:p0`,
`:p1` placeholders:

```yaml
- action: sql
  params:
    connection_id: main
    query: "SELECT * FROM users WHERE email = :p0"
    params: ["alice@example.com"]
```

## Configuration via setup steps

```yaml
tools:
  modules:
    database:
      setup:                       # run at module load
        - action: connect
          params:
            connection_id: main
            driver: sqlite
            database: ./data.db
            policy: { preset: safe_write }
      constraints:
        allowed_actions: [connect, fetch_results, list_tables, sql, schema]
```

## Cross-references

- App-config block reference (`tools.modules.database`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- RAG Text2SQL strategy uses this module for SQL execution:
  [RAG → Text2SQL](../../language/37-rag.md#text2sql)
- Examples (analyst pattern, enterprise multi-source):
  [Examples](../../language/15-examples.md) (7, 10, 11)
