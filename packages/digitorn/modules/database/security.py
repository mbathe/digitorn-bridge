"""Database module - Security policy, query guard, and audit logger.

Three layers of database-specific security:

1. **DatabasePolicy** - per-connection security policy (Pydantic model,
   YAML/JSON/dashboard-configurable). Controls what operations are allowed
   on each connection.

2. **QueryGuard** - runtime interceptor that validates every query/operation
   against the policy BEFORE it reaches the adapter. Raises
   ``QueryBlockedError`` on violation.

3. **AuditLogger** - structured log of every database operation with full
   context (who, what, when, how long, how many rows).

YAML configuration example::

    database:
      connections:
        main:
          driver: postgresql
          host: localhost
          database: myapp
          persist: true
          policy:
            read_only: false
            blocked_statements: [DROP, TRUNCATE, ALTER]
            table_blacklist: [audit_log, secrets]
            column_blacklist:
              users: [password_hash, ssn]
            max_rows_returned: 5000
            max_query_time_seconds: 30
        analytics:
          driver: postgresql
          host: analytics-host
          database: analytics
          policy:
            read_only: true
            max_rows_returned: 50000
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


class DatabasePolicy(BaseModel):
    """Per-connection security policy.

    Serializable to/from YAML, JSON, or dashboard forms. Every field has
    a description so both LLM agents and human admins understand it.

    Apply to a connection via ``ConnectParams.policy`` or update at runtime
    via the ``set_policy`` action.
    """


    read_only: bool = Field(
        default=False,
        description=(
            "If true, block ALL write operations (INSERT, UPDATE, DELETE, "
            "CREATE, ALTER, DROP, TRUNCATE). Only SELECT and introspection "
            "are allowed. Ideal for analytics or reporting connections."
        ),
    )


    allowed_statements: list[str] = Field(
        default_factory=list,
        description=(
            "Whitelist of allowed SQL statement types (e.g. ['SELECT', 'INSERT']). "
            "Empty list means ALL statements are allowed (subject to other rules). "
            "Takes precedence over blocked_statements."
        ),
    )

    blocked_statements: list[str] = Field(
        default_factory=list,
        description=(
            "Blacklist of blocked SQL statement types. "
            "Common values: ['DROP', 'TRUNCATE', 'ALTER', 'GRANT', 'REVOKE']. "
            "Ignored if allowed_statements is non-empty."
        ),
    )


    table_whitelist: list[str] = Field(
        default_factory=list,
        description=(
            "If non-empty, ONLY these tables are accessible. "
            "Supports glob patterns (e.g. 'app_*', 'public.*'). "
            "All other tables are hidden from introspection and blocked in queries."
        ),
    )

    table_blacklist: list[str] = Field(
        default_factory=list,
        description=(
            "Tables that are blocked from all access. "
            "Supports glob patterns (e.g. 'audit_*', 'internal_*'). "
            "Applied after table_whitelist."
        ),
    )


    column_blacklist: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Columns hidden from introspection and redacted in query results. "
            "Format: {table_name: [column_name, ...]}. "
            "Example: {'users': ['password_hash', 'ssn', 'credit_card']}. "
            "Blocked columns appear as '***REDACTED***' in fetch results."
        ),
    )


    max_rows_returned: int = Field(
        default=10_000,
        ge=1,
        le=100_000,
        description=(
            "Maximum number of rows any SELECT/fetch can return. "
            "Overrides the per-query limit if lower. "
            "Protects against accidental data exfiltration."
        ),
    )

    max_query_time_seconds: float = Field(
        default=30.0,
        ge=0.1,
        le=300.0,
        description=(
            "Maximum execution time for any query in seconds. "
            "The query is cancelled if it exceeds this limit."
        ),
    )


    allow_transactions: bool = Field(
        default=True,
        description=(
            "If false, BEGIN/COMMIT/ROLLBACK are blocked. "
            "Useful for connections where auto-commit is preferred."
        ),
    )

    max_transaction_seconds: float = Field(
        default=300.0,
        ge=1.0,
        le=3600.0,
        description=(
            "Maximum duration for an explicit transaction. "
            "Auto-rolled back if exceeded."
        ),
    )


    blocked_operations: list[str] = Field(
        default_factory=list,
        description=(
            "MongoDB operations to block. "
            "Example: ['drop_collection', 'delete_many', 'update_many']. "
            "Applied in addition to read_only checks."
        ),
    )


    blocked_commands: list[str] = Field(
        default_factory=list,
        description=(
            "Redis commands to block (case-insensitive). "
            "Example: ['FLUSHDB', 'FLUSHALL', 'CONFIG', 'DEBUG', 'EVAL', 'SCRIPT']. "
            "Applied in addition to read_only checks."
        ),
    )


    @classmethod
    def readonly(cls) -> DatabasePolicy:
        """Preset: read-only connection (introspection + SELECT only)."""
        return cls(read_only=True)

    @classmethod
    def safe_write(cls) -> DatabasePolicy:
        """Preset: allow reads + safe writes, block destructive DDL."""
        return cls(
            blocked_statements=["DROP", "TRUNCATE", "ALTER", "GRANT", "REVOKE"],
            blocked_operations=["drop_collection"],
            blocked_commands=["FLUSHDB", "FLUSHALL", "CONFIG", "DEBUG", "EVAL", "SCRIPT"],
        )

    @classmethod
    def unrestricted(cls) -> DatabasePolicy:
        """Preset: no restrictions (admin/dev use only)."""
        return cls()


class QueryBlockedError(Exception):
    """Raised when the QueryGuard blocks a query based on policy."""

    def __init__(self, reason: str, *, policy_rule: str, detail: str = "") -> None:
        self.reason = reason
        self.policy_rule = policy_rule
        self.detail = detail
        super().__init__(reason)


_WRITE_STATEMENTS = frozenset({
    "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP",
    "TRUNCATE", "GRANT", "REVOKE", "MERGE", "UPSERT", "REPLACE",
})

_READ_STATEMENTS = frozenset({"SELECT", "SHOW", "DESCRIBE", "EXPLAIN", "WITH"})

_MONGO_WRITE_OPS = frozenset({
    "insert_one", "insert_many",
    "update_one", "update_many",
    "delete_one", "delete_many",
    "drop_collection", "create_collection",
})

_REDIS_WRITE_COMMANDS = frozenset({
    "SET", "SETNX", "SETEX", "PSETEX", "MSET", "MSETNX", "APPEND",
    "INCR", "INCRBY", "INCRBYFLOAT", "DECR", "DECRBY",
    "HSET", "HMSET", "HDEL", "HINCRBY", "HINCRBYFLOAT",
    "LPUSH", "RPUSH", "LPOP", "RPOP", "LSET", "LINSERT", "LREM", "LTRIM",
    "SADD", "SREM", "SPOP", "SMOVE",
    "ZADD", "ZREM", "ZINCRBY", "ZRANGESTORE",
    "DEL", "UNLINK", "RENAME", "RENAMENX", "EXPIRE", "EXPIREAT",
    "PEXPIRE", "PEXPIREAT", "PERSIST",
    "FLUSHDB", "FLUSHALL",
    "XADD", "XDEL", "XTRIM",
    "EVAL", "EVALSHA", "SCRIPT",
    "CONFIG",
})


class QueryGuard:
    """Validates queries/operations against a DatabasePolicy before execution.

    Usage::

        guard = QueryGuard(policy)

        guard.check_sql_query("SELECT * FROM users WHERE id = :p0")
        guard.check_sql_query("DROP TABLE users")

        guard.check_mongo_operation("users", "delete_many")

        guard.check_redis_command("FLUSHDB")
    """

    def __init__(self, policy: DatabasePolicy) -> None:
        self._policy = policy
        self._allowed = {s.upper() for s in policy.allowed_statements}
        self._blocked = {s.upper() for s in policy.blocked_statements}
        self._blocked_ops = {s.lower() for s in policy.blocked_operations}
        self._blocked_cmds = {s.upper() for s in policy.blocked_commands}

    @property
    def policy(self) -> DatabasePolicy:
        return self._policy


    def check_sql_query(self, query: str) -> None:
        """Validate a SQL query against the policy. Raises QueryBlockedError."""
        stmt_type = _extract_statement_type(query)

        if self._policy.read_only and stmt_type in _WRITE_STATEMENTS:
            raise QueryBlockedError(
                f"Write operation '{stmt_type}' blocked: connection is read-only.",
                policy_rule="read_only",
                detail=f"statement={stmt_type}",
            )

        if self._allowed:
            if stmt_type not in self._allowed:
                raise QueryBlockedError(
                    f"Statement '{stmt_type}' not in allowed list: {sorted(self._allowed)}.",
                    policy_rule="allowed_statements",
                    detail=f"statement={stmt_type}",
                )
        elif self._blocked:
            if stmt_type in self._blocked:
                raise QueryBlockedError(
                    f"Statement '{stmt_type}' is blocked by policy.",
                    policy_rule="blocked_statements",
                    detail=f"statement={stmt_type}",
                )

        tables = _extract_table_names(query)
        for table in tables:
            self.check_table_access(table)

    def check_table_access(self, table: str) -> None:
        """Check if a table is accessible under the policy."""
        import fnmatch

        if self._policy.table_whitelist:
            if not any(
                fnmatch.fnmatch(table, pattern)
                for pattern in self._policy.table_whitelist
            ):
                raise QueryBlockedError(
                    f"Table '{table}' not in whitelist.",
                    policy_rule="table_whitelist",
                    detail=f"table={table}, whitelist={self._policy.table_whitelist}",
                )

        if self._policy.table_blacklist:
            if any(
                fnmatch.fnmatch(table, pattern)
                for pattern in self._policy.table_blacklist
            ):
                raise QueryBlockedError(
                    f"Table '{table}' is blacklisted.",
                    policy_rule="table_blacklist",
                    detail=f"table={table}",
                )

    def check_column_access(self, table: str, columns: list[str]) -> list[str]:
        """Filter columns, returning only accessible ones.

        Blocked columns are replaced with None in the result (for redaction).
        Returns the list of blocked column names.
        """
        blocked = self._policy.column_blacklist.get(table, [])
        if not blocked:
            return []
        return [c for c in columns if c in blocked]

    def get_effective_row_limit(self, requested_limit: int) -> int:
        """Return the effective row limit (min of requested and policy max)."""
        return min(requested_limit, self._policy.max_rows_returned)

    def get_query_timeout(self) -> float:
        """Return the max query execution time in seconds."""
        return self._policy.max_query_time_seconds

    def get_transaction_timeout(self) -> float:
        """Return the max transaction duration in seconds."""
        return self._policy.max_transaction_seconds


    def check_mongo_operation(
        self, collection: str, operation: str,
    ) -> None:
        """Validate a MongoDB operation against the policy."""
        if self._policy.read_only and operation.lower() in _MONGO_WRITE_OPS:
            raise QueryBlockedError(
                f"Write operation '{operation}' blocked: connection is read-only.",
                policy_rule="read_only",
                detail=f"operation={operation}, collection={collection}",
            )

        if operation.lower() in self._blocked_ops:
            raise QueryBlockedError(
                f"Operation '{operation}' is blocked by policy.",
                policy_rule="blocked_operations",
                detail=f"operation={operation}, collection={collection}",
            )

        self.check_table_access(collection)


    def check_redis_command(self, command: str) -> None:
        """Validate a Redis command against the policy."""
        cmd = command.upper()

        if self._policy.read_only and cmd in _REDIS_WRITE_COMMANDS:
            raise QueryBlockedError(
                f"Write command '{cmd}' blocked: connection is read-only.",
                policy_rule="read_only",
                detail=f"command={cmd}",
            )

        if cmd in self._blocked_cmds:
            raise QueryBlockedError(
                f"Command '{cmd}' is blocked by policy.",
                policy_rule="blocked_commands",
                detail=f"command={cmd}",
            )


    def check_transaction_allowed(self) -> None:
        """Check if transactions are allowed by policy."""
        if not self._policy.allow_transactions:
            raise QueryBlockedError(
                "Transactions are disabled on this connection.",
                policy_rule="allow_transactions",
            )


@dataclass
class AuditEntry:
    """A single audit log entry for a database operation."""

    timestamp: float
    connection_id: str
    action: str
    driver: str
    query_preview: str
    tables_affected: list[str]
    rows_affected: int
    duration_seconds: float
    blocked: bool = False
    block_reason: str = ""
    app_id: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class AuditLogger:
    """Structured audit logger for database operations.

    Logs every database action with full context for compliance,
    debugging, and anomaly detection.

    Entries are emitted via structlog and optionally stored in a buffer
    for programmatic access (e.g. dashboard, export).
    """

    def __init__(self, max_buffer_size: int = 1000) -> None:
        self._buffer: list[AuditEntry] = []
        self._max_buffer_size = max_buffer_size

    def log(
        self,
        *,
        connection_id: str,
        action: str,
        driver: str = "",
        query: str = "",
        tables: list[str] | None = None,
        rows_affected: int = 0,
        duration: float = 0.0,
        blocked: bool = False,
        block_reason: str = "",
        app_id: str = "",
        session_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """Record a database operation."""
        entry = AuditEntry(
            timestamp=time.time(),
            connection_id=connection_id,
            action=action,
            driver=driver,
            query_preview=_sanitize_query(query),
            tables_affected=tables or [],
            rows_affected=rows_affected,
            duration_seconds=round(duration, 4),
            blocked=blocked,
            block_reason=block_reason,
            app_id=app_id,
            session_id=session_id,
            metadata=metadata or {},
        )

        self._buffer.append(entry)
        if len(self._buffer) > self._max_buffer_size:
            self._buffer = self._buffer[-self._max_buffer_size:]

        log_fn = logger.warning if blocked else logger.info
        log_fn(
            "db_audit",
            connection_id=connection_id,
            action=action,
            driver=driver,
            query_preview=entry.query_preview,
            tables=entry.tables_affected,
            rows_affected=rows_affected,
            duration=entry.duration_seconds,
            blocked=blocked,
            block_reason=block_reason or None,
            app_id=app_id or None,
        )

        return entry

    def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent audit entries as dicts (for API/dashboard)."""
        entries = self._buffer[-limit:]
        return [
            {
                "timestamp": e.timestamp,
                "connection_id": e.connection_id,
                "action": e.action,
                "driver": e.driver,
                "query_preview": e.query_preview,
                "tables_affected": e.tables_affected,
                "rows_affected": e.rows_affected,
                "duration_seconds": e.duration_seconds,
                "blocked": e.blocked,
                "block_reason": e.block_reason,
                "app_id": e.app_id,
            }
            for e in reversed(entries)
        ]

    def get_stats(self) -> dict[str, Any]:
        """Return aggregate audit statistics."""
        total = len(self._buffer)
        blocked = sum(1 for e in self._buffer if e.blocked)
        by_action: dict[str, int] = {}
        by_connection: dict[str, int] = {}
        for e in self._buffer:
            by_action[e.action] = by_action.get(e.action, 0) + 1
            by_connection[e.connection_id] = by_connection.get(e.connection_id, 0) + 1
        return {
            "total_operations": total,
            "blocked_operations": blocked,
            "by_action": by_action,
            "by_connection": by_connection,
        }

    def clear(self) -> None:
        """Clear the audit buffer."""
        self._buffer.clear()


_REDACTED = "***REDACTED***"


def redact_columns(
    rows: list[dict[str, Any]],
    blocked_columns: list[str],
) -> list[dict[str, Any]]:
    """Replace values of blocked columns with REDACTED placeholder."""
    if not blocked_columns:
        return rows
    blocked_set = set(blocked_columns)
    return [
        {
            k: _REDACTED if k in blocked_set else v
            for k, v in row.items()
        }
        for row in rows
    ]


def filter_table_list(
    tables: list[Any],
    guard: QueryGuard,
) -> list[Any]:
    """Filter a list of TableInfo objects based on table access policy."""
    result = []
    for table in tables:
        try:
            guard.check_table_access(table.name)
            result.append(table)
        except QueryBlockedError:
            continue
    return result


_STMT_PATTERN = re.compile(
    r"^\s*(?:--[^\n]*\n\s*|/\*.*?\*/\s*)*"
    r"(\w+)",
    re.IGNORECASE | re.DOTALL,
)

_TABLE_PATTERN = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE|TABLE|TRUNCATE)\s+"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
    r"[`\"\[]?(\w+(?:\.\w+)?)[`\"\]]?",
    re.IGNORECASE,
)


def _extract_statement_type(query: str) -> str:
    """Extract the SQL statement type (SELECT, INSERT, DROP, etc.)."""
    match = _STMT_PATTERN.match(query)
    if match:
        return match.group(1).upper()
    return "UNKNOWN"


def _extract_table_names(query: str) -> list[str]:
    """Extract table names from a SQL query (best-effort regex)."""
    return list(set(_TABLE_PATTERN.findall(query)))


def _sanitize_query(query: str, max_length: int = 200) -> str:
    """Truncate and sanitize a query for audit logging."""
    clean = " ".join(query.split())
    if len(clean) > max_length:
        return clean[:max_length] + "..."
    return clean


_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def validate_sql_identifier(name: str, label: str = "identifier") -> str:
    """Validate and return a safe SQL identifier.

    Raises ``QueryBlockedError`` if the identifier contains anything
    other than ``[A-Za-z0-9_]`` (optionally dot-separated for schema.table).

    This prevents SQL injection via table/column names in dynamically
    built queries (bulk_insert, upsert, etc.).
    """
    if not name or not isinstance(name, str):
        raise QueryBlockedError(
            f"Empty or invalid {label}: {name!r}",
            policy_rule="identifier_validation",
        )
    parts = name.split(".")
    if len(parts) > 2:
        raise QueryBlockedError(
            f"Invalid {label} (too many dots): {name!r}",
            policy_rule="identifier_validation",
        )
    for part in parts:
        if not _SAFE_IDENTIFIER_RE.match(part):
            raise QueryBlockedError(
                f"Unsafe {label}: {name!r} - only letters, digits, and underscores allowed",
                policy_rule="identifier_validation",
            )
    return name


def validate_sql_identifiers(names: list[str], label: str = "identifier") -> list[str]:
    """Validate a list of SQL identifiers. Returns the list if all are safe."""
    for name in names:
        validate_sql_identifier(name, label)
    return names
