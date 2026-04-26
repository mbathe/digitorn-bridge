---
id: module-concept-database
title: "database module — overview"
type: module-concept
module: database
isolation: shared
keywords: [database, database-module, connect, disconnect, list_connections, bulk_insert, transaction, sql, schema, browse, relations, search_data]
version: 1.0.0
---

# `database` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 10 visible, 6 internal

## Description (from class docstring)

Database module — multi-driver async database access for LLM agents.

Supports any SQL database via SQLAlchemy async drivers (PostgreSQL, MySQL,
SQLite, MSSQL, Oracle). Provides named connections, schema introspection,
query execution, explicit transactions, and bulk inserts.

LLM-facing actions (10 — these appear in the agent's tool list):
  - connect          Open a named database connection
  - disconnect       Close a connection (or all)
  - list_connections List active connections
  - sql              Universal SQL — SELECT, DML, DDL, EXPLAIN
  - transaction      Explicit BEGIN/COMMIT/ROLLBACK on a connection
  - bulk_insert      Fast multi-row insert
  - schema           Explore tables / columns / FK / sample data
  - browse           Paginated row browsing for one table
  - relations        Show foreign key relationships for a table
  - search_data      Search data in a table by column value

Internal actions (6 — registered for bus.call() but hidden from the LLM):
  - execute_query, fetch_results, list_tables, describe, introspect,
    extract_for_index. The RAG / index modules call these directly via the
    service bus; LLM agents go through the higher-level wrappers above.

## Configuration

Set under `modules.database.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |
| `annotations` | dict |  | `{}` | Table/column annotations used for LLM hints. |
| `auto_connect` | list |  | `[]` | Connections to establish at module start. |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `connect` | `DbConnect` |  | medium | Open a named database connection. Supports SQLite, PostgreSQL, MySQL, MSSQL, and Oracle. The connection_id is used to... |
| `disconnect` | `DbDisconnect` |  | low | Close a named database connection. Use connection_id='*' to close all. Example: disconnect(connection_id='main') |
| `list_connections` | `DbList` |  | low | List all active database connections with metadata. Example: list_connections() |
| `execute_query` | `DatabaseExecuteQuery` | ✓ | high | Internal: execute a DML/DDL statement. Hidden from LLM agents — the RAG module calls this via the bus, agents should ... |
| `bulk_insert` | `DbBulkInsert` |  | medium | Insert many rows into a table in one optimized call (batched, atomic). Use this instead of looping over sql() — much ... |
| `fetch_results` | `DatabaseFetchResults` | ✓ | low | Internal: execute a SELECT query and return rows. Hidden from LLM agents — the RAG module calls this via the bus, age... |
| `list_tables` | `DatabaseListTables` | ✓ | low | Internal: list tables with columns/FK/indexes. Hidden from LLM agents — called internally by schema() and by the RAG/... |
| `introspect` | `DatabaseIntrospect` | ✓ | low | Internal: full schema introspection. Hidden from LLM agents — called internally by schema(what='all') and by the RAG ... |
| `describe` | `DatabaseDescribe` | ✓ | low | Internal: full table context (schema + sample + stats). Hidden from LLM agents — called internally by schema(what='de... |
| `transaction` | `DbTransaction` |  | medium | Control an explicit database transaction. Use op='begin' to open, op='commit' to persist, op='rollback' to undo. All ... |
| `extract_for_index` | `DatabaseExtractForIndex` | ✓ | low | Internal: extract schema as IndexEntry + Relation data for the index module. Hidden from LLM agents — called automati... |
| `sql` | `DbQuery` |  | medium | Universal SQL execution. Auto-detects query type: SELECT/WITH/SHOW/EXPLAIN/PRAGMA return rows; INSERT/UPDATE/DELETE r... |
| `schema` | `DbSchema` |  | low | Explore database schema. Use what='tables' to list, what='describe' for one table in detail (columns, types, FK, inde... |
| `browse` | `DbBrowse` |  | low | Browse a table interactively with pagination. Like scrolling through a spreadsheet. Shows rows with column names. Exa... |
| `relations` | `DbRelations` |  | low | Show foreign key relationships for a table. Reveals how tables are connected — essential for writing JOINs. Example: ... |
| `search_data` | `DbSearch` |  | low | Search for data in a table by column value. Supports exact match and partial match (LIKE). Like Ctrl+F in a spreadshe... |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: database
      actions: [connect, disconnect, list_connections, bulk_insert, transaction, sql, schema, browse, relations, search_data]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {database: [connect, disconnect, list_connections, bulk_insert, transaction]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/database-*.md`.
