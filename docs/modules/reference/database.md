---
id: database
title: Database Module
sidebar_label: database
sidebar_position: 8
description: Multi-driver async SQL — 16 actions covering connections, queries, schema introspection, transactions, and bulk ops.
---

# database

Multi-driver async SQL database module. Named connections, query execution, full schema introspection, transactions, bulk insert, per-row browsing, FK-aware relations, full-text data search.

| Property | Value |
|----------|-------|
| **Module ID** | `database` |
| **Version** | `1.0.0` |
| **Type** | database |
| **Platforms** | All |
| **Async drivers** | `aiosqlite`, `asyncpg`, `aiomysql`, `aioodbc`, `oracledb` |
| **Short names (LLM)** | `DbConnect`, `DbDisconnect`, `DbList`, `DbQuery`, `DbTransaction`, `DbBulkInsert`, `DbSchema`, `DbBrowse`, `DbRelations`, `DbSearch` |

---

## Actions (16)

### Connection management (3)

| Action | Short name | Purpose |
|--------|------------|---------|
| `connect` | `DbConnect` | Open named connection (`driver`, `database`, `host`, `port`, `username`, `password_env`, `policy`, `role`, `group`, `persist`, `options`) |
| `disconnect` | `DbDisconnect` | Close one connection or all with `*` |
| `list_connections` | `DbList` | Active connections + metadata |

### Query execution (4)

| Action | Short name | Purpose |
|--------|------------|---------|
| `sql` | `DbQuery` | **Recommended** — universal query (SELECT / INSERT / UPDATE / DELETE / DDL) with auto-detect + safety LIMIT injection for SELECT |
| `execute_query` | — | Raw SQL execution (DDL/DML). Parameterized binding. |
| `fetch_results` | — | SELECT with explicit LIMIT |
| `transaction` | `DbTransaction` | Run a list of queries atomically (begin/commit/rollback managed) |

### Schema introspection (5)

| Action | Short name | Purpose |
|--------|------------|---------|
| `schema` | `DbSchema` | **Recommended** — unified exploration. `what: "tables" \| "describe" \| "all"` |
| `list_tables` | — | List tables with columns + indexes |
| `describe` | — | Full table context: schema + samples |
| `introspect` | — | Full schema dump (all tables) |
| `relations` | `DbRelations` | FK graph: which tables reference which |

### Data inspection (2)

| Action | Short name | Purpose |
|--------|------------|---------|
| `browse` | `DbBrowse` | Paginated row browse for a table |
| `search_data` | `DbSearch` | Full-text / LIKE search across columns |

### Bulk & index integration (2)

| Action | Short name | Purpose |
|--------|------------|---------|
| `bulk_insert` | `DbBulkInsert` | Insert many rows in one call |
| `extract_for_index` | — | Extract schema for the `index` module |

---

## Recommended agent surface

For most apps, grant only the two "smart" actions and `connect`:

```yaml
capabilities:
  grant:
    - module: database
      actions: [connect, sql, schema]
```
The `sql` action auto-injects a LIMIT clause on unbounded SELECT queries and validates syntax before executing. The `schema` action dispatches `tables`/`describe`/`all` via its `what` param.

---

## Read-replica routing

Connections with the same `group` and different `role` values form a replica set:

```yaml
- action: connect
  params:
    connection_id: main_db
    role: primary
    group: main
- action: connect
  params:
    connection_id: main_replica
    role: replica
    group: main
```
Read queries against the group are distributed across replicas via round-robin.

---

## Security policies

Per-connection policies control what queries are allowed:

| Preset | Meaning |
|--------|---------|
| `readonly` | Only SELECT allowed |
| `safe_write` | SELECT + INSERT/UPDATE, no DDL |
| `unrestricted` | All queries allowed |

Pass `policy` on `connect`:

```yaml
modules:
  database:
    config:
      connections:
        - connection_id: main
          driver: postgres
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
---

## Constraints

| Constraint | Type | Description |
|------------|------|-------------|
| `allowed_hosts` | list[str] | Allowed database hosts. Only localhost is allowed by default. |
| `allowed_actions` | list[str] | Restrict which database actions are exposed. |
| `blocked_actions` | list[str] | Block specific actions. |

```yaml
modules:
  database:
    constraints:
      allowed_hosts: [localhost, db.internal]
      allowed_actions: [connect, sql, schema, browse]
```
---

## SQL injection prevention

All query actions use parameterized binding — never interpolate untrusted values into SQL strings. Use `:p0`, `:p1` placeholders:

```yaml
- action: sql
  params:
    connection_id: main
    query: "SELECT * FROM users WHERE email = :p0"
    params: ["alice@example.com"]
```