---

id: database
title: Database Module
sidebar_position: 12
format: md
---




# database

Direct SQL database operations with support for SQLite, PostgreSQL, and MySQL. Provides connection management, raw SQL execution, convenience CRUD operations, and transaction control.

| Property | Value |
|----------|-------|
| **Module ID** | `database` |
| **Version** | `1.0.0` |
| **Type** | database |
| **Platforms** | All |
| **Dependencies** | None (SQLite via stdlib). `psycopg2` for PostgreSQL, `mysql-connector-python` for MySQL. |
| **Declared Permissions** | `database.read`, `database.write` |

---


## Actions (13)

### Connection Management

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `connect` | Open database connection | `connection_id`, `database`, `driver` (`sqlite`, `postgresql`, `mysql`), `host`, `port`, `username`, `password` |
| `disconnect` | Close connection | `connection_id` |

### Query Execution

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `execute_query` | Raw SQL (DDL, DML) | `connection_id`, `query`, `parameters` |
| `fetch_results` | SELECT with metadata | `connection_id`, `query`, `parameters`, `limit` |

**Security**:
- `@requires_permission(Permission.DATABASE_WRITE)` for `execute_query`
- `@audit_trail("detailed")` for `execute_query`

All queries use parameterized binding — SQL injection is prevented by design.

### CRUD Operations

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `insert_record` | Insert single record | `connection_id`, `table`, `data` |
| `update_record` | Update by condition | `connection_id`, `table`, `data`, `condition` |
| `delete_record` | Delete by condition | `connection_id`, `table`, `condition` |
| `create_table` | Create table with schema | `connection_id`, `table`, `columns` |

**Security for delete_record**:
- `@requires_permission(Permission.DATABASE_WRITE)`
- `@sensitive_action(RiskLevel.HIGH, irreversible=True)`
- `@audit_trail("detailed")`

### Schema Introspection

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `list_tables` | List all tables | `connection_id` |
| `get_table_schema` | Columns, types, constraints | `connection_id`, `table` |

### Transaction Control

| Action | Description | Key Parameters |
|--------|-------------|----------------|
| `begin_transaction` | Start transaction | `connection_id` |
| `commit_transaction` | Commit | `connection_id` |
| `rollback_transaction` | Rollback | `connection_id` |

---


## Implementation Notes

- Named connection IDs allow multiple concurrent database connections
- Per-connection threading locks for thread safety
- SQLite: WAL mode enabled, foreign key enforcement on
- Autocommit mode with explicit transaction control
- Parameterized queries only — string interpolation is never used
