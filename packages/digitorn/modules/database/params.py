"""Database module — Pydantic parameter models for all actions.

Every field has a description so LLM agents know exactly what to pass.
Each model maps 1:1 to an action in DatabaseModule.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class ConnectParams(BaseModel):
    """Open a named database connection.

    Examples:
        connect(connection_id="main", driver="sqlite", database="app.db")
        connect(connection_id="prod", driver="postgresql", host="localhost", database="myapp", username="admin", password_env="DB_PASSWORD")
        connect(connection_id="analytics", driver="postgresql", url="postgresql+asyncpg://user:pass@host/db")
    """

    model_config = {"populate_by_name": True, "extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def _accept_type_alias(cls, data: Any) -> Any:
        # BUG-082: some docs say ``type:``, some say ``driver:``. Map
        # ``type`` → ``driver`` BEFORE the field-level validator fires
        # so the user-facing error message matches the field they
        # actually used and both wordings are equivalent.
        if isinstance(data, dict) and "driver" not in data and "type" in data:
            data = {**data, "driver": data["type"]}
        return data

    connection_id: str = Field(
        ...,
        description=(
            "Unique name for this connection (e.g. 'main', 'analytics', 'crm'). "
            "Used to reference the connection in all subsequent actions."
        ),
    )
    # BUG-082: older docs + several app.yaml examples use ``type:``
    # instead of ``driver:``. Accept either field name, raising a
    # clear error that matches the field the caller actually used.
    driver: str = Field(
        ...,
        description=(
            "Database driver: 'sqlite', 'postgresql', 'mysql', 'mssql', 'oracle', "
            "'mongodb', 'redis'. Alias: ``type``."
        ),
        pattern="^(sqlite|postgresql|mysql|mssql|oracle|mongodb|redis)$",
    )
    database: str | None = Field(
        None,
        description=(
            "Database name or file path. Required for all drivers except SQLite "
            "(which defaults to ':memory:')."
        ),
    )
    host: str | None = Field(
        None, description="Database host. Defaults to 'localhost' for network databases.",
    )
    port: int | None = Field(
        None, ge=1, le=65535, description="Database port. Uses driver default if omitted.",
    )
    username: str | None = Field(None, description="Authentication username.")
    password: str | None = Field(None, description="Authentication password.")
    url: str | None = Field(
        None,
        description=(
            "Full SQLAlchemy async URL (e.g. 'postgresql+asyncpg://user:pass@host/db'). "
            "If provided, overrides host/port/database/username/password."
        ),
    )
    options: dict[str, Any] | None = Field(
        None,
        description=(
            "Engine options: pool_size (default 5), max_overflow (default 10), "
            "pool_recycle (default 3600), echo (default false)."
        ),
    )
    persist: bool = Field(
        False,
        description=(
            "If true, save this connection config so it auto-reconnects on daemon restart. "
            "Passwords are stored as env var references (e.g. '$DB_PASSWORD'), never in plaintext."
        ),
    )
    password_env: str | None = Field(
        None,
        description=(
            "Environment variable name containing the password (e.g. 'DB_PASSWORD'). "
            "Used for persistent connections instead of storing plaintext passwords."
        ),
    )
    policy: dict[str, Any] | None = Field(
        None,
        description=(
            "Security policy for this connection. Controls what operations are allowed. "
            "Keys: read_only (bool), blocked_statements (list), table_whitelist (list), "
            "table_blacklist (list), column_blacklist (dict), max_rows_returned (int), "
            "max_query_time_seconds (float), allow_transactions (bool), "
            "blocked_operations (list, MongoDB), blocked_commands (list, Redis). "
            "Use presets: {'preset': 'readonly'}, {'preset': 'safe_write'}, or custom dict."
        ),
    )
    role: str | None = Field(
        None,
        description=(
            "Connection role for read replica routing: 'primary' (read+write), "
            "'replica' (read-only, auto-routed for SELECT queries). "
            "Omit for standalone connections."
        ),
        pattern="^(primary|replica)$",
    )
    group: str | None = Field(
        None,
        description=(
            "Connection group name for replica routing. Connections in the same group "
            "are treated as a primary + replica set. "
            "E.g. group='production' with role='primary' and role='replica'."
        ),
    )


class DisconnectParams(BaseModel):
    """Close a named database connection."""

    connection_id: str = Field(
        ..., description="Connection to close. Use '*' to close all connections.",
    )


class ListConnectionsParams(BaseModel):
    """List all active database connections."""


class ExecuteQueryParams(BaseModel):
    """Execute a DML/DDL statement (INSERT, UPDATE, DELETE, CREATE, ALTER, DROP).

    Examples:
        execute_query(connection_id="main", query="INSERT INTO users (name, email) VALUES (:p0, :p1)", params=["Alice", "alice@example.com"])
        execute_query(connection_id="main", query="UPDATE users SET active = :p0 WHERE id = :p1", params=[true, 42])
        execute_query(connection_id="main", query="CREATE TABLE logs (id INTEGER PRIMARY KEY, message TEXT)")
    """

    connection_id: str = Field(
        ..., description="Connection to execute on.",
    )
    query: str = Field(
        ...,
        description=(
            "SQL statement to execute. Use :p0, :p1, :p2 for parameter placeholders."
        ),
    )
    params: list[Any] | None = Field(
        None,
        description="Positional parameters for the query (mapped to :p0, :p1, :p2, ...).",
    )


class BulkInsertParams(BaseModel):
    """Insert multiple rows into a table in a single optimized call.

    Much faster than individual INSERT statements. Handles value formatting
    automatically — just provide column names and a list of row arrays.
    """

    connection_id: str = Field(
        ..., description="Connection to insert into.",
    )
    table: str = Field(
        ..., description="Table name to insert into.",
    )
    columns: list[str] = Field(
        ...,
        description="Column names in insertion order (e.g. ['name', 'email', 'age']).",
        min_length=1,
    )
    rows: list[list[Any]] = Field(
        ...,
        description=(
            "List of rows to insert. Each row is a list of values matching "
            "the columns order. Example: [['Alice', 'alice@example.com', 30], "
            "['Bob', 'bob@example.com', 25]]. "
            "For very large imports (>50k rows), call bulk_insert multiple times."
        ),
        min_length=1,
        max_length=50000,
    )


class FetchResultsParams(BaseModel):
    """Execute a SELECT query and return rows.

    Examples:
        fetch_results(connection_id="main", query="SELECT * FROM users WHERE active = :p0", params=[true], limit=10)
        fetch_results(connection_id="main", query="SELECT name, email FROM users ORDER BY created_at DESC", limit=5)
    """

    connection_id: str = Field(
        ..., description="Connection to query on.",
    )
    query: str = Field(
        ..., description="SELECT query to execute.",
    )
    params: list[Any] | None = Field(
        None,
        description="Positional parameters for the query (mapped to :p0, :p1, :p2, ...).",
    )
    limit: int = Field(
        1000, ge=1, le=50000,
        description="Maximum number of rows to return. A safety LIMIT is injected if missing.",
    )


class ListTablesParams(BaseModel):
    """List all tables in a database with column and relationship metadata.

    Examples:
        list_tables(connection_id="main")
        list_tables(connection_id="main", schema_name="public")
    """

    connection_id: str = Field(
        ..., description="Connection to introspect.",
    )
    schema_name: str | None = Field(
        None, description="Database schema to inspect (e.g. 'public'). Defaults to the default schema.",
    )


class IntrospectParams(BaseModel):
    """Full schema introspection — all tables, columns, FK, indexes in one call."""

    connection_id: str = Field(
        ..., description="Connection to introspect.",
    )
    schema_name: str | None = Field(
        None, description="Database schema to inspect. Defaults to the default schema.",
    )


class DescribeParams(BaseModel):
    """Full context for a table — schema + stats + sample + FK in one call.

    This is the primary action for LLM agents. Call this first to understand
    a table before writing queries.

    Examples:
        describe(connection_id="main", table="users")
        describe(connection_id="main", table="orders", sample_limit=10)
    """

    connection_id: str = Field(
        ..., description="Connection to query.",
    )
    table: str = Field(
        ..., description="Table to describe.",
    )
    sample_limit: int = Field(
        5, ge=0, le=50,
        description="Number of sample rows to include. Set to 0 to skip sampling.",
    )


class TransactionParams(BaseModel):
    """Control an explicit database transaction.

    Use this when you need ATOMICITY across multiple SQL statements: open
    a transaction with op='begin', run as many sql() / bulk_insert() calls
    as you need on the same connection_id, then either op='commit' to
    persist the changes or op='rollback' to undo everything.

    Rules:
    - Only one transaction per connection at a time. Calling 'begin' twice
      without commit/rollback returns an error.
    - All sql() and bulk_insert() calls that target the same connection_id
      automatically run inside the open transaction — no extra parameter
      needed.
    - Transactions auto-rollback on disconnect, on session end, and after
      a configurable timeout (default 300s) so a forgotten commit cannot
      lock the database forever.
    - If a sql() call fails inside a transaction, the transaction is NOT
      automatically rolled back — you can still react and commit/rollback
      explicitly.
    """

    connection_id: str = Field(
        "default",
        description="Connection to control. Use the same id you passed to sql().",
    )
    op: str = Field(
        ...,
        description=(
            "Operation: 'begin' to open a transaction, 'commit' to persist "
            "changes, 'rollback' to undo all uncommitted changes."
        ),
        pattern="^(begin|commit|rollback)$",
    )


class ExtractForIndexParams(BaseModel):
    """Extract IndexEntry + Relation data for the index module.

    Called by ``index.scan`` via the service bus when scanning a database source.
    """

    source_id: str = Field(
        ..., description="Source ID as registered in the index module.",
    )
    root: str = Field(
        ..., description="Connection ID to introspect (passed as 'root' by the index).",
    )
    force: bool = Field(
        default=False, description="Force full re-extraction.",
    )


# ── Simplified actions (LLM-friendly) ──────────────────────────
#
# 10 actions are exposed to the LLM:
#   connect, disconnect, list_connections — connection lifecycle
#   sql                                   — universal SQL (SELECT/DML/DDL/EXPLAIN)
#   transaction                           — explicit BEGIN/COMMIT/ROLLBACK
#   bulk_insert                           — fast multi-row insert
#   schema, browse, relations, search_data — exploration helpers
#
# 5 actions remain in the registry but are marked internal=True so the LLM
# never sees them — they exist solely so the RAG / index modules can call
# them via bus.call("database", "<name>", ...):
#   execute_query, fetch_results, describe, introspect, list_tables,
#   extract_for_index


class SchemaParams(BaseModel):
    """Explore database schema — tables, columns, types, relationships, sample data.

    This is the FIRST action to call after connect(). Understand the database
    structure before writing any SQL.

    Three modes:
    - what="tables" — list all tables with column count and row count (START HERE)
    - what="describe" — full detail for ONE table: columns, types, FK, indexes, sample rows
    - what="all" — full schema dump of all tables (use sparingly on large databases)

    Workflow:
    1. schema(what="tables") → see all tables
    2. schema(what="describe", table="users") → understand "users" table
    3. sql(query="SELECT ...") → write queries based on what you learned

    Examples:
        schema(connection_id="main", what="tables")
        schema(connection_id="main", what="describe", table="users")
        schema(connection_id="main", what="describe", table="orders")
        schema(connection_id="main", what="all")
    """
    connection_id: str = Field(
        default="default",
        description="Connection to explore. Default: 'default'. Example: 'main'",
    )
    what: str = Field(
        default="tables",
        description="Scope: 'tables' (list all — start here), 'describe' (one table in detail), 'all' (full dump).",
        pattern="^(tables|describe|all)$",
    )
    table: str | None = Field(
        default=None,
        description="Table name — required when what='describe'. Example: 'users'",
    )


class SqlParams(BaseModel):
    """Execute any SQL query — the universal database action.

    Auto-detects query type:
    - SELECT / WITH / SHOW / EXPLAIN / PRAGMA → returns rows as a list of dicts
    - INSERT / UPDATE / DELETE                → returns affected row count
    - CREATE / ALTER / DROP                   → returns success confirmation

    Companion actions (use them when they fit):
    - bulk_insert(...)   for inserting many rows efficiently
    - schema(...)        to explore tables and columns
    - browse(...)        for paginated row preview
    - search_data(...)   for simple column lookups
    - relations(...)     to discover foreign keys
    - transaction(op=…)  to wrap multi-step changes in an atomic transaction

    Tips:
    - Always add LIMIT to SELECT queries (default cap: 100 rows)
    - Use parameterized queries: sql(query="SELECT * FROM users WHERE id = :p0", params=[42])
    - sql() inside an open transaction automatically runs in that transaction

    Examples:
        sql(query="SELECT * FROM users LIMIT 10")
        sql(query="SELECT u.name, count(o.id) as orders FROM users u LEFT JOIN orders o ON u.id = o.user_id GROUP BY u.name ORDER BY orders DESC LIMIT 10")
        sql(query="INSERT INTO users (name, email) VALUES ('Alice', 'alice@example.com')")
        sql(query="UPDATE users SET active = false WHERE last_login < '2024-01-01'")
        sql(query="DELETE FROM sessions WHERE expired_at < datetime('now')")
        sql(query="CREATE TABLE logs (id INTEGER PRIMARY KEY, message TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        sql(query="EXPLAIN SELECT * FROM orders WHERE user_id = 42")
        sql(query="SELECT * FROM users WHERE email = :p0", params=["alice@example.com"])
        sql(query="SELECT count(*) FROM orders WHERE status = 'pending'", connection_id="analytics")
    """
    query: str = Field(
        ...,
        description="Any SQL query. SELECT returns rows, DML returns affected count. Always add LIMIT to SELECT.",
    )
    params: list[Any] | None = Field(
        None,
        description="Positional parameters for :p0, :p1, :p2 placeholders. Example: ['alice@example.com', 42]",
    )
    connection_id: str = Field(
        default="default",
        description="Connection to use. Default: 'default'",
    )


class BrowseParams(BaseModel):
    """Paginated table navigation."""
    table: str = Field(..., description="Table name to browse.")
    page: int = Field(default=1, ge=1, description="Page number (1-indexed).")
    per_page: int = Field(default=20, ge=1, le=100, description="Rows per page.")
    connection_id: str = Field(default="default", description="Connection to use.")


class RelationsParams(BaseModel):
    """Show FK relationships for a table."""
    table: str = Field(..., description="Table name to inspect.")
    connection_id: str = Field(default="default", description="Connection to use.")


class SearchDataParams(BaseModel):
    """Search data in a table by column value."""
    table: str = Field(..., description="Table name to search.")
    column: str = Field(..., description="Column to search in.")
    value: str = Field(default="", description="Value to search for.")
    mode: str = Field(default="contains", description="Search mode: exact, contains, starts_with, ends_with.")
    limit: int = Field(default=20, ge=1, le=100, description="Max results to return.")
    connection_id: str = Field(default="default", description="Connection to use.")
