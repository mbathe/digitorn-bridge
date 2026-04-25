# Database Module — Action Reference

The database module exposes **10 actions** to LLM agents and keeps **6 internal
actions** in the registry for the RAG / index modules. Internal actions are
hidden from the LLM tool list and discovery search, but remain callable via
`bus.call("database", "<name>", ...)`.

---

## LLM-visible actions (10)

### connect

Open a named database connection with optional replica routing.

**Parameters:**
- `connection_id` (required): Unique name for this connection.
- `driver` (required): `sqlite`, `postgresql`, `mysql`, `mssql`, or `oracle`.
- `database`: Database name or file path.
- `host`: Database host (default: `localhost`).
- `port`: Database port.
- `username`: Authentication username.
- `password`: Authentication password.
- `url`: Full SQLAlchemy async URL (overrides individual params).
- `options`: Engine options (`pool_size`, `max_overflow`, `pool_recycle`, `echo`).
- `persist`: Save config for auto-reconnect on daemon restart.
- `password_env`: Env var name for the password (used with `persist`).
- `policy`: Security policy dict or preset (`readonly`, `safe_write`, `unrestricted`).
- `role`: Connection role: `primary` or `replica` (for read replica routing).
- `group`: Connection group name (connections in the same group form a primary + replica set).

### disconnect

Close a named connection. Pass `connection_id="*"` to close all.

### list_connections

List all active connections with metadata (driver, URL, age, connected status).

### sql

Universal SQL execution. Auto-detects query type:
- `SELECT` / `WITH` / `SHOW` / `EXPLAIN` / `PRAGMA` → returns rows as a list of dicts
- `INSERT` / `UPDATE` / `DELETE` → returns affected row count
- `CREATE` / `ALTER` / `DROP` → returns success

**Parameters:**
- `query` (required): Any SQL statement.
- `params`: Positional parameters bound to `:p0`, `:p1`, …
- `connection_id`: Connection to use (default `"default"`).

When called inside an open transaction (see below), `sql()` automatically runs
in that transaction — no extra parameter needed.

### transaction

Control an explicit database transaction.

**Parameters:**
- `connection_id` (default `"default"`): Connection to control.
- `op` (required): `"begin"`, `"commit"`, or `"rollback"`.

**Workflow:**
```
transaction(connection_id='main', op='begin')
sql(query='UPDATE accounts SET balance = balance - 100 WHERE id = 1', connection_id='main')
sql(query='UPDATE accounts SET balance = balance + 100 WHERE id = 2', connection_id='main')
transaction(connection_id='main', op='commit')   # or 'rollback'
```

**Rules:**
- Only one open transaction per connection at a time
- All `sql()` and `bulk_insert()` calls on the same `connection_id` run in the open transaction
- Auto-rollback on disconnect / session end / timeout (default 300s)
- A failing `sql()` inside a transaction does NOT auto-rollback — you decide

### bulk_insert

Insert many rows into a table efficiently.

**Parameters:**
- `connection_id` (required): Connection to insert into.
- `table` (required): Target table name.
- `columns` (required): Column names in insertion order.
- `rows` (required): List of row arrays (max 50 000 rows per call).

Internally batched in chunks of 500 rows. Atomic (all rows or none). When
called inside an open transaction, runs in that transaction.

### schema

Explore database schema in one call.

**Parameters:**
- `connection_id` (default `"default"`): Connection to explore.
- `what` (default `"tables"`): `"tables"`, `"describe"`, or `"all"`.
- `table`: Table name — required when `what="describe"`.

### browse

Paginated row preview for one table.

**Parameters:**
- `table` (required): Table name.
- `page` (default `1`): Page number.
- `per_page` (default `20`, max `100`): Rows per page.
- `connection_id` (default `"default"`): Connection to use.

### relations

Show foreign key relationships for a table — both outgoing FKs and incoming
references from other tables.

**Parameters:**
- `table` (required): Table name.
- `connection_id` (default `"default"`): Connection to use.

### search_data

Search a table by column value.

**Parameters:**
- `table` (required): Table to search.
- `column` (required): Column to search in.
- `value` (default `""`): Value to search for.
- `mode` (default `"contains"`): `"exact"`, `"contains"`, `"starts_with"`, `"ends_with"`.
- `limit` (default `20`, max `100`): Max rows to return.
- `connection_id` (default `"default"`): Connection to use.

---

## Internal actions (6 — not exposed to LLM)

These actions remain in `_action_registry` and are routable via `bus.call()`,
but they have `internal=True` on the `@action` decorator so they never appear
in the LLM tool list or the discovery search index. They are kept because the
RAG / index modules call them directly:

| Action | Used by |
|--------|---------|
| `execute_query` | `rag.indexing.sync` (DDL/DML/triggers) |
| `fetch_results` | `rag.indexing.engine`, `rag.indexing.sync`, `rag.strategies.text2sql` |
| `list_tables` | `database.schema()` wrapper (internal call) |
| `introspect` | `database.schema(what='all')`, `rag.module` |
| `describe` | `database.schema(what='describe')`, `rag.indexing.engine` |
| `extract_for_index` | `index.scan` |

LLM agents should never reference these — use the wrappers above instead.
