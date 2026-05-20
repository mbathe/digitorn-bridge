"""Database module - Pydantic parameter models for all actions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

class ConnectParams(BaseModel):
    """Open a named database connection."""

    model_config = {"populate_by_name": True, "extra": "allow"}

    @model_validator(mode="before")
    @classmethod
    def _accept_type_alias(cls, data: Any) -> Any:
        # Accept `type:` as an alias for `driver:` before field validation.
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
    driver: str = Field(
        ...,
        description=(
            "Database driver: 'sqlite', 'postgresql', 'mysql', 'mssql', 'oracle', "
            "'mongodb', 'redis'. Alias: `type`."
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
    """Execute a DML/DDL statement (INSERT, UPDATE, DELETE, CREATE, ALTER, DROP)."""

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
    """Insert multiple rows into a table in a single optimized call."""

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
    """Execute a SELECT query and return rows."""

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
    """List all tables in a database with column and relationship metadata."""

    connection_id: str = Field(
        ..., description="Connection to introspect.",
    )
    schema_name: str | None = Field(
        None, description="Database schema to inspect (e.g. 'public'). Defaults to the default schema.",
    )

class IntrospectParams(BaseModel):
    """Full schema introspection - all tables, columns, FK, indexes in one call."""

    connection_id: str = Field(
        ..., description="Connection to introspect.",
    )
    schema_name: str | None = Field(
        None, description="Database schema to inspect. Defaults to the default schema.",
    )

class DescribeParams(BaseModel):
    """Full context for a table - schema + stats + sample + FK in one call."""

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
    """Control an explicit database transaction."""

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
    """Extract IndexEntry + Relation data for the index module."""

    source_id: str = Field(
        ..., description="Source ID as registered in the index module.",
    )
    root: str = Field(
        ..., description="Connection ID to introspect (passed as 'root' by the index).",
    )
    force: bool = Field(
        default=False, description="Force full re-extraction.",
    )

class SchemaParams(BaseModel):
    """Explore database schema - tables, columns, types, relationships, sample data."""
    connection_id: str = Field(
        default="default",
        description="Connection to explore. Default: 'default'. Example: 'main'",
    )
    what: str = Field(
        default="tables",
        description="Scope: 'tables' (list all - start here), 'describe' (one table in detail), 'all' (full dump).",
        pattern="^(tables|describe|all)$",
    )
    table: str | None = Field(
        default=None,
        description="Table name - required when what='describe'. Example: 'users'",
    )

class SqlParams(BaseModel):
    """Execute any SQL query - the universal database action."""
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
