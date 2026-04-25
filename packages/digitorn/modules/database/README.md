# Database Module

Multi-driver async database module for the Digitorn platform.

## Overview

A small surface of high-power actions designed to give an LLM agent everything
it needs without flooding the tool list. Supports any SQL database via
SQLAlchemy async drivers.

Supported drivers:

| Driver | Backend | Async Driver |
|--------|---------|-------------|
| `sqlite` | SQLite | aiosqlite |
| `postgresql` | PostgreSQL | asyncpg |
| `mysql` | MySQL | aiomysql |
| `mssql` | SQL Server | aioodbc |
| `oracle` | Oracle | oracledb |

## Actions exposed to LLM agents (10)

| Action | Description | Risk | Permissions |
|--------|-------------|------|-------------|
| **Connection lifecycle** | | | |
| `connect` | Open a named database connection | Medium | `database:admin` |
| `disconnect` | Close a connection (or all with `*`) | Low | `database:admin` |
| `list_connections` | List active connections with metadata | Low | `database:read` |
| **Query** | | | |
| `sql` | Universal SQL — SELECT, INSERT, UPDATE, DELETE, DDL, EXPLAIN | Medium | `database:write` |
| `transaction` | Explicit `BEGIN` / `COMMIT` / `ROLLBACK` on a connection | Medium | `database:write` |
| `bulk_insert` | Fast multi-row insert (batched, atomic) | Medium | `database:write` |
| **Exploration** | | | |
| `schema` | Tables / columns / FK / sample (`tables`, `describe`, `all`) | Low | `database:read` |
| `browse` | Paginated row browsing for one table | Low | `database:read` |
| `relations` | Show foreign key relationships for a table | Low | `database:read` |
| `search_data` | Search a table by column value (exact / contains / starts_with) | Low | `database:read` |

## Internal actions (called by RAG / index modules via the bus)

These actions remain in the registry but are marked `internal=True` so they
are hidden from the LLM tool list. They exist solely so other modules can
call them directly via `bus.call("database", "<name>", ...)`. Agents should
use the higher-level wrappers above instead.

| Action | Used by | Why kept |
|--------|---------|----------|
| `execute_query` | RAG sync (DDL/DML/triggers) | DDL operations during indexing |
| `fetch_results` | RAG indexing engine, sync, text2sql | Polling and SQL execution |
| `list_tables` | `schema()` wrapper | Internal helper |
| `introspect` | `schema(what='all')`, RAG router | Schema dump |
| `describe` | `schema(what='describe')`, RAG indexing | Per-table metadata |
| `extract_for_index` | `index.scan` | Schema → IndexEntry conversion |

## Transactions

`transaction(connection_id, op)` is the **only** way the agent controls
explicit transactions. Pattern:

```
transaction(connection_id='main', op='begin')
sql(query='UPDATE accounts SET balance = balance - 100 WHERE id = 1', connection_id='main')
sql(query='UPDATE accounts SET balance = balance + 100 WHERE id = 2', connection_id='main')
# All good?
transaction(connection_id='main', op='commit')
# Otherwise:
transaction(connection_id='main', op='rollback')
```

Rules:
- Only one open transaction per connection at a time
- All `sql()` and `bulk_insert()` calls on the same `connection_id` automatically run inside the open transaction
- Forgotten commits auto-rollback after the transaction timeout (default 300s)
- A failing `sql()` inside a transaction does **not** auto-rollback — the agent decides
- On disconnect or session end, an open transaction is rolled back automatically

## Architecture

```
  DatabaseModule
       │
       ├── ConnectionPool    (named connections)
       │       │
       │       ├── ConnectionEntry
       │       │       │
       │       │       └── DatabaseAdapter  (protocol)
       │       │               │
       │       │               └── SQLAdapter  (SQLAlchemy async)
       │       │
       │       └── ReplicaGroup  (primary + replicas, round-robin)
       │
       ├── SecurityPolicy    (per-connection allow/block rules)
       │
       ├── AnnotationStore   (business context for tables/columns)
       │
       └── Watcher integration
               │
               └── list_items() + checksum()  →  PollingWatcher
```

## App YAML Configuration

The database module is fully configurable via the Digitorn app YAML system.
See [docs/app-config.yaml](docs/app-config.yaml) for a complete reference.

Example:

```yaml
modules:
  database:
    setup:
      - action: connect
        params:
          connection_id: main_db
          driver: postgresql
          host: "{{env.DB_HOST}}"
          database: my_app
          username: "{{env.DB_USER}}"
          password_env: DB_PASSWORD
          policy: safe_write
          role: primary
          group: main

      - action: set_policy
        params:
          connection_id: main_db
          policy:
            preset: safe_write
            allowed_tables: [users, orders, products]
            blocked_tables: [credentials, audit_log]

    constraints:
      allowed_actions: [sql, schema, browse, relations, search_data]
      blocked_actions: []
```

## LLM Usage

Recommended workflow:

```
1. database.connect                           →  open a connection
2. database.schema(what='tables')             →  see all tables
3. database.schema(what='describe', table=…)  →  understand a table
4. database.sql(query='SELECT … LIMIT 10')    →  read data
5. database.transaction(op='begin') + sql()…  →  atomic multi-step changes
6. database.bulk_insert(rows=[…])             →  fast ingestion
```
