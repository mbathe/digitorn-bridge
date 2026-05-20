"""Database module - multi-driver async database access for LLM agents."""

from __future__ import annotations


import logging

logger = logging.getLogger(__name__)
import asyncio
import os
import time as _time
from dataclasses import asdict
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from digitorn.modules.base import ActionResult, BaseModule, Platform
from digitorn.modules.manifest import ConstraintSpec
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest

from .cache import SchemaCache
from .connections import ConnectionPool
from .params import (
    BrowseParams,
    BulkInsertParams,
    ConnectParams,
    DescribeParams,
    DisconnectParams,
    ExecuteQueryParams,
    ExtractForIndexParams,
    FetchResultsParams,
    IntrospectParams,
    ListConnectionsParams,
    ListTablesParams,
    RelationsParams,
    SchemaParams,
    SearchDataParams,
    SqlParams,
    TransactionParams,
)
from .security import (
    AuditLogger,
    DatabasePolicy,
    QueryBlockedError,
    QueryGuard,
    filter_table_list,
    redact_columns,
    validate_sql_identifier,
    validate_sql_identifiers,
)

logger = structlog.get_logger(__name__)

class DatabaseConfig(BaseModel):
    """Pydantic config for the database module (validated at compile time)."""

    model_config = {"extra": "forbid"}

    workspace: str = Field(default="", description="Auto-injected by the daemon.")
    annotations: dict[str, Any] = Field(
        default_factory=dict,
        description="Table/column annotations used for LLM hints.",
    )
    auto_connect: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Connections to establish at module start.",
    )

class DatabaseModule(BaseModule):
    MODULE_ID = "database"
    VERSION = "1.0.0"
    SUPPORTED_PLATFORMS = [Platform.ALL]
    MODULE_TYPE = "system"
    CONFIG_MODEL = DatabaseConfig

    CONSTRAINTS = [
        ConstraintSpec(
            name="allowed_hosts",
            type="string_list",
            description="Allowed database hosts. Only localhost is allowed by default.",
            scope="universal",
        ),
        ConstraintSpec(
            name="allowed_actions",
            type="string_list",
            description="Restrict which database actions are available.",
            scope="universal",
        ),
        ConstraintSpec(
            name="blocked_actions",
            type="string_list",
            description="Block specific database actions.",
            scope="universal",
        ),
    ]

    _QUERY_HISTORY_MAX = 500

    def __init__(self) -> None:
        super().__init__()
        self.pool = ConnectionPool()
        self.audit = AuditLogger()
        self.schema_cache = SchemaCache(ttl=300.0)
        self._annotations: dict[str, dict[str, dict[str, Any]]] = {}
        self._saved_configs: list[dict[str, Any]] = []
        self._tx_start_times: dict[str, float] = {}
        self._query_history: list[dict[str, Any]] = []
        self._connection_groups: dict[str, dict[str, Any]] = {}
        self._replica_rr: dict[str, int] = {}

    def get_prompt_sections(self) -> list[dict[str, Any]]:
        """Inject database context into the LLM system prompt."""
        connections = self.pool.list_connections()
        if not connections:
            return []

        lines = [
            "You have database access. The 10 actions below are everything "
            "you need - there is no other database action.",
            "",
            "## Exploration (start here)",
            "- schema(what='tables') - list all tables",
            "- schema(what='describe', table='users') - full detail on one table",
            "- relations(table='orders') - show foreign keys",
            "- browse(table='users', page=1) - paginated row preview",
            "- search_data(table='users', column='email', value='@gmail.com') - find rows by value",
            "",
            "## Reading and writing",
            "- sql(query='...') - universal SQL: SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, EXPLAIN",
            "- bulk_insert(table='...', columns=[...], rows=[[...], [...]]) - fast multi-row insert",
            "",
            "## Atomic transactions (when several changes must succeed or fail together)",
            "- transaction(connection_id='...', op='begin')   # open",
            "- sql(...) / bulk_insert(...)                    # any number of writes",
            "- transaction(connection_id='...', op='commit')  # persist",
            "- transaction(connection_id='...', op='rollback')# undo everything",
            "",
            "## Connection lifecycle (rarely needed - connections are usually pre-configured)",
            "- connect / disconnect / list_connections",
            "",
            "## Active connections",
        ]
        for conn in connections:
            cid = conn.get("id", "?")
            driver = conn.get("driver", "?")
            db = conn.get("database", "?")
            lines.append(f"  - {cid}: {driver} ({db})")

        # Show cached table names if available
        for conn in connections:
            cid = conn.get("id", "?")
            cached = self.schema_cache.get_tables(cid)
            tables = [getattr(t, "name", str(t)) for t in cached] if cached else None
            if tables:
                table_list = ", ".join(tables[:20])
                if len(tables) > 20:
                    table_list += f", ... ({len(tables) - 20} more)"
                lines.append(f"  Tables in '{cid}': {table_list}")

        lines.extend([
            "",
            "## Tips",
            "- ALWAYS add LIMIT to SELECT queries (default cap: 100 rows)",
            "- Call schema(what='describe', table='...') before writing complex queries",
            "- Use sql('EXPLAIN ...') to check the query plan before slow queries",
            "- Parameterize: sql(query='SELECT * FROM users WHERE id = :p0', params=[42])",
            "- Wrap multi-step changes in transaction(op='begin') ... transaction(op='commit')",
        ])

        return [{
            "title": "Database",
            "content": "\n".join(lines),
            "priority": 30,
        }]

    async def on_start(self) -> None:
        pass

    async def on_config_update(self, config: dict[str, Any]) -> None:
        yaml_annotations = config.get("annotations", {})
        if yaml_annotations:
            for table_key, table_info in yaml_annotations.items():
                parts = table_key.rsplit(".", 1)
                table_name = parts[-1]
                conn_id = parts[0] if len(parts) > 1 else "_default"

                if conn_id not in self._annotations:
                    self._annotations[conn_id] = {}
                if table_name not in self._annotations[conn_id]:
                    self._annotations[conn_id][table_name] = {}

                if isinstance(table_info, str):
                    self._annotations[conn_id][table_name]["_description"] = table_info
                elif isinstance(table_info, dict):
                    desc = table_info.get("description", "")
                    if desc:
                        self._annotations[conn_id][table_name]["_description"] = desc
                    columns = table_info.get("columns", {})
                    for col_name, col_desc in columns.items():
                        self._annotations[conn_id][table_name][col_name] = col_desc

            logger.info(
                "database_annotations_loaded tables=%d",
                len(yaml_annotations),
            )

        auto = config.get("auto_connect", [])
        for cfg in auto:
            conn_id = cfg.get("connection_id", "default")
            if conn_id in self.pool._connections:
                continue
            try:
                await self.pool.connect(
                    connection_id=conn_id,
                    driver=cfg["driver"],
                    host=cfg.get("host"),
                    port=cfg.get("port"),
                    database=cfg.get("database"),
                    username=cfg.get("username"),
                    password=cfg.get("password"),
                    url=cfg.get("url"),
                )
                logger.info("database_auto_connected id=%s", conn_id)
            except Exception as exc:
                logger.warning("database_auto_connect_failed id=%s error=%s", conn_id, exc, exc_info=True)

        await super().on_config_update(config)

    async def on_stop(self) -> None:
        closed = await self.pool.disconnect_all()
        if closed:
            await logger.ainfo("database_module_stopped", connections_closed=closed)

    def state_snapshot(self) -> dict[str, Any]:
        """Persist connection configs + annotations + policies for daemon restart."""
        return {
            "connections": self.pool.list_connections(),
            "saved_configs": self._saved_configs,
            "annotations": self._annotations,
            "query_history": self._query_history[-100:],
            "connection_groups": self._connection_groups,
        }

    def _get_guard(self, connection_id: str) -> QueryGuard | None:
        entry = self.pool._connections.get(connection_id)
        if entry and entry.guard:
            return entry.guard
        return None

    _DEFAULT_QUERY_TIMEOUT: float = 30.0
    _DEFAULT_TX_TIMEOUT: float = 300.0

    def _get_query_timeout(self, guard: QueryGuard | None) -> float:
        if guard:
            return guard.get_query_timeout() or self._DEFAULT_QUERY_TIMEOUT
        return self._DEFAULT_QUERY_TIMEOUT

    async def _run_with_timeout(
        self, coro: Any, timeout: float | None,
    ) -> Any:
        if timeout:
            return await asyncio.wait_for(coro, timeout=timeout)
        return await coro

    def _check_transaction_timeout(self, connection_id: str, guard: QueryGuard | None) -> None:
        start = self._tx_start_times.get(connection_id)
        if start is None:
            return
        elapsed = _time.monotonic() - start
        max_seconds = (
            guard.get_transaction_timeout()
            if guard
            else self._DEFAULT_TX_TIMEOUT
        )
        if elapsed > max_seconds:
            raise QueryBlockedError(
                f"Transaction exceeded time limit ({elapsed:.1f}s > {max_seconds:.0f}s). "
                "Auto-rollback required.",
                policy_rule="max_transaction_seconds",
                detail=f"elapsed={elapsed:.1f}s, limit={max_seconds:.0f}s",
            )

    def _record_query(
        self,
        connection_id: str,
        query: str,
        *,
        success: bool = True,
        rows_returned: int | None = None,
        rows_affected: int | None = None,
        duration: float = 0.0,
        error: str | None = None,
    ) -> None:
        import datetime
        entry = {
            "connection_id": connection_id,
            "query": query[:2000],
            "success": success,
            "duration_ms": round(duration * 1000, 1),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if rows_returned is not None:
            entry["rows_returned"] = rows_returned
        if rows_affected is not None:
            entry["rows_affected"] = rows_affected
        if error:
            entry["error"] = error[:500]
        self._query_history.append(entry)
        if len(self._query_history) > self._QUERY_HISTORY_MAX:
            self._query_history = self._query_history[-self._QUERY_HISTORY_MAX:]

    def _resolve_connection_for_read(self, connection_id: str) -> str:
        entry = self.pool._connections.get(connection_id)
        if not entry:
            return connection_id

        for group_name, group in self._connection_groups.items():
            if connection_id == group.get("primary") or connection_id in group.get("replicas", []):
                replicas = group.get("replicas", [])
                live_replicas = [r for r in replicas if r in self.pool._connections]
                if live_replicas:
                    idx = self._replica_rr.get(group_name, 0) % len(live_replicas)
                    self._replica_rr[group_name] = idx + 1
                    return live_replicas[idx]
        return connection_id

    async def _ensure_schema_cached(self, connection_id: str) -> list[Any]:
        if not self.schema_cache.has(connection_id):
            adapter = self.pool.get_adapter(connection_id)
            schema = await adapter.introspect()
            self.schema_cache.store(
                connection_id, tables=schema.tables, schema_info=schema,
            )
            return schema.tables
        return self.schema_cache.get_tables(connection_id)  # type: ignore[return-value]

    async def restore_state(self, state: dict[str, Any]) -> None:
        """Auto-reconnect persistent connections and restore annotations."""
        self._annotations = state.get("annotations", {})
        self._query_history = state.get("query_history", [])
        self._connection_groups = state.get("connection_groups", {})

        saved = state.get("saved_configs", [])
        self._saved_configs = saved
        for cfg in saved:
            try:
                password = _resolve_secret(cfg.get("password_env"))
                entry = await self.pool.connect(
                    connection_id=cfg["connection_id"],
                    driver=cfg["driver"],
                    database=cfg.get("database"),
                    host=cfg.get("host"),
                    port=cfg.get("port"),
                    username=cfg.get("username"),
                    password=password,
                    url=cfg.get("url"),
                    options=cfg.get("options"),
                )
                policy_data = cfg.get("policy")
                if policy_data:
                    policy = _resolve_policy(policy_data)
                    entry.guard = QueryGuard(policy)

                await logger.ainfo(
                    "connection_restored",
                    connection_id=cfg["connection_id"],
                    driver=cfg["driver"],
                    policy_applied=bool(policy_data),
                )
            except Exception as exc:
                await logger.awarning(
                    "connection_restore_failed",
                    connection_id=cfg["connection_id"],
                    error=str(exc),
                    exc_info=True,
                )

    @action(
        description=(
            "Open a named database connection. Supports SQLite, PostgreSQL, "
            "MySQL, MSSQL, and Oracle. The connection_id is used to reference "
            "this connection in all subsequent actions. Example: connect(connection_id='main', driver='sqlite', database='data.db')"
        ),
        params_model=ConnectParams,
        permissions=["database:admin"],
        risk_level="medium",
        tags=["connection", "lifecycle"],
        aliases=["connecter", "ouvrir", "connexion", "base", "bdd"],
        cli_label='Connect',
        cli_param='database',
    )
    async def connect(self, params: ConnectParams) -> ActionResult:
        _ctx = getattr(self, "_context", None)
        _allowed_hosts = None
        if _ctx and hasattr(_ctx, "constraints"):
            _allowed_hosts = _ctx.constraints.get("allowed_hosts")
        try:
            entry = await self.pool.connect(
                connection_id=params.connection_id,
                driver=params.driver,
                database=params.database,
                host=params.host,
                port=params.port,
                username=params.username,
                password=params.password,
                url=params.url,
                options=params.options,
                allowed_hosts=_allowed_hosts,
            )

            policy_data = params.policy
            policy_applied = False
            if policy_data:
                policy = _resolve_policy(policy_data)
                entry.guard = QueryGuard(policy)
                policy_applied = True

            persistent = False
            if params.persist:
                persistent = True
                self._saved_configs = [
                    c for c in self._saved_configs
                    if c["connection_id"] != params.connection_id
                ]
                self._saved_configs.append({
                    "connection_id": params.connection_id,
                    "driver": params.driver,
                    "database": params.database,
                    "host": params.host,
                    "port": params.port,
                    "username": params.username,
                    "password_env": params.password_env,
                    "url": params.url,
                    "options": params.options,
                    "policy": policy_data,
                })

            if params.group and params.role:
                group = self._connection_groups.setdefault(
                    params.group, {"primary": None, "replicas": []},
                )
                if params.role == "primary":
                    group["primary"] = params.connection_id
                elif params.role == "replica":
                    if params.connection_id not in group["replicas"]:
                        group["replicas"].append(params.connection_id)

            return ActionResult(
                success=True,
                data={
                    "connection_id": entry.connection_id,
                    "driver": entry.driver,
                    "url": entry.url_display,
                    "persistent": persistent,
                    "policy_applied": policy_applied,
                    "role": params.role,
                    "group": params.group,
                    "message": f"Connected to {entry.driver} as '{entry.connection_id}'.",
                },
            )
        except (ValueError, SQLAlchemyError) as exc:
            return ActionResult(success=False, error=str(exc))

    @action(
        description="Close a named database connection. Use connection_id='*' to close all. Example: disconnect(connection_id='main')",
        params_model=DisconnectParams,
        permissions=["database:admin"],
        risk_level="low",
        tags=["connection", "lifecycle"],
        cli_label="Disconnect",
        cli_param="connection_id",
    )
    async def disconnect(self, params: DisconnectParams) -> ActionResult:
        if params.connection_id == "*":
            closed = await self.pool.disconnect_all()
            self._saved_configs.clear()
            self.schema_cache.invalidate_all()
            return ActionResult(
                success=True,
                data={"closed": closed, "message": f"Closed {closed} connection(s)."},
            )

        if params.connection_id not in self.pool:
            return ActionResult(
                success=False,
                error=f"Connection '{params.connection_id}' not found.",
            )

        await self.pool.disconnect(params.connection_id)
        self.schema_cache.invalidate(params.connection_id)
        self._saved_configs = [
            c for c in self._saved_configs
            if c["connection_id"] != params.connection_id
        ]
        return ActionResult(
            success=True,
            data={
                "connection_id": params.connection_id,
                "message": f"Connection '{params.connection_id}' closed.",
            },
        )

    @action(
        description="List all active database connections with metadata. Example: list_connections()",
        params_model=ListConnectionsParams,
        permissions=["database:read"],
        risk_level="low",
        tags=["connection", "info"],
        cli_label="Connections",
    )
    async def list_connections(self, params: ListConnectionsParams) -> ActionResult:
        connections = self.pool.list_connections()
        return ActionResult(
            success=True,
            data={
                "connections": connections,
                "count": len(connections),
            },
        )

    @action(
        description=(
            "Internal: execute a DML/DDL statement. Hidden from LLM agents - "
            "the RAG module calls this via the bus, agents should use sql() instead."
        ),
        params_model=ExecuteQueryParams,
        permissions=["database:write"],
        risk_level="high",
        tags=["query", "write", "internal"],
        internal=True,
    )
    async def execute_query(self, params: ExecuteQueryParams) -> ActionResult:
        t0 = _time.monotonic()
        guard = self._get_guard(params.connection_id)
        try:
            if guard:
                entry = self.pool.get(params.connection_id)
                if entry.driver in ("mongodb", "redis"):
                    pass
                else:
                    guard.check_sql_query(params.query)

            self._check_transaction_timeout(params.connection_id, guard)

            adapter = self.pool.get_adapter(params.connection_id)
            timeout = self._get_query_timeout(guard)
            result = await self._run_with_timeout(
                adapter.execute(params.query, params.params), timeout,
            )

            from .security import _extract_statement_type
            stmt = _extract_statement_type(params.query)
            if stmt in ("CREATE", "ALTER", "DROP", "TRUNCATE"):
                self.schema_cache.invalidate(params.connection_id)

            duration = _time.monotonic() - t0
            self.audit.log(
                connection_id=params.connection_id,
                action="execute_query",
                driver=self.pool.get(params.connection_id).driver,
                query=params.query,
                rows_affected=result.rows_affected,
                duration=duration,
            )
            self._record_query(
                params.connection_id, params.query,
                rows_affected=result.rows_affected, duration=duration,
            )

            return ActionResult(
                success=True,
                data={
                    "rows_affected": result.rows_affected,
                    "last_insert_id": result.last_insert_id,
                },
            )
        except asyncio.TimeoutError:
            duration = _time.monotonic() - t0
            self.audit.log(
                connection_id=params.connection_id,
                action="execute_query",
                query=params.query,
                blocked=True,
                block_reason="query_timeout",
                duration=duration,
            )
            self._record_query(
                params.connection_id, params.query,
                success=False, error="query_timeout", duration=duration,
            )
            timeout_val = self._get_query_timeout(guard)
            return ActionResult(
                success=False,
                error=f"Query timed out after {timeout_val}s (max_query_time_seconds).",
            )
        except QueryBlockedError as exc:
            self.audit.log(
                connection_id=params.connection_id,
                action="execute_query",
                query=params.query,
                blocked=True,
                block_reason=exc.reason,
                duration=_time.monotonic() - t0,
            )
            return ActionResult(
                success=False,
                error=f"Blocked by policy ({exc.policy_rule}): {exc.reason}",
            )
        except KeyError as exc:
            return ActionResult(success=False, error=str(exc))
        except SQLAlchemyError as exc:
            return ActionResult(success=False, error=f"Query failed: {exc}")

    @action(
        description=(
            "Insert many rows into a table in one optimized call (batched, atomic). "
            "Use this instead of looping over sql() - much faster and cheaper in tokens "
            "for large inserts. Example: bulk_insert(table='users', columns=['name','email'], "
            "rows=[['Alice','a@x.com'],['Bob','b@x.com']])."
        ),
        tool_prompt=(
            "Insert many rows into a table efficiently.\n"
            "\n"
            "## When to use\n"
            "- Inserting more than ~10 rows: bulk_insert is much faster than calling sql() in a loop\n"
            "- CSV / JSON ingestion\n"
            "- Seeding test data\n"
            "\n"
            "## Format\n"
            "  bulk_insert(\n"
            "    table='users',\n"
            "    columns=['name', 'email', 'age'],\n"
            "    rows=[\n"
            "      ['Alice', 'alice@x.com', 30],\n"
            "      ['Bob',   'bob@x.com',   25],\n"
            "    ],\n"
            "  )\n"
            "\n"
            "## Notes\n"
            "- Atomic: all rows succeed or all are rolled back\n"
            "- Internally batched in chunks of 500 rows\n"
            "- Inside an open transaction, runs in that transaction (no extra commit)\n"
            "- Max 50 000 rows per call - split larger imports across multiple calls\n"
        ),
        params_model=BulkInsertParams,
        permissions=["database:write"],
        risk_level="medium",
        tags=["query", "write", "insert", "bulk"],
        aliases=["inserer_lot", "inserer_plusieurs", "import"],
        cli_label='Insert',
        cli_param='table',
    )
    async def bulk_insert(self, params: BulkInsertParams) -> ActionResult:
        t0 = _time.monotonic()
        guard = self._get_guard(params.connection_id)
        try:
            validate_sql_identifier(params.table, "table name")
            validate_sql_identifiers(params.columns, "column name")

            n_cols = len(params.columns)
            for i, row in enumerate(params.rows):
                if len(row) != n_cols:
                    return ActionResult(
                        success=False,
                        error=(
                            f"Row {i} has {len(row)} values but {n_cols} columns "
                            f"were specified ({', '.join(params.columns)})."
                        ),
                    )

            if guard:
                check_query = f"INSERT INTO {params.table} ({', '.join(params.columns)}) VALUES ()"
                guard.check_sql_query(check_query)

            adapter = self.pool.get_adapter(params.connection_id)
            timeout = self._get_query_timeout(guard)

            placeholders = ", ".join(f":p{i}" for i in range(n_cols))
            insert_sql = f"INSERT INTO {params.table} ({', '.join(params.columns)}) VALUES ({placeholders})"

            param_dicts = [
                {f"p{i}": v for i, v in enumerate(row)}
                for row in params.rows
            ]

            _BATCH_SIZE = 500
            await adapter.begin()
            total_affected = 0
            try:
                for offset in range(0, len(param_dicts), _BATCH_SIZE):
                    batch = param_dicts[offset : offset + _BATCH_SIZE]
                    result = await self._run_with_timeout(
                        adapter.execute_many(insert_sql, batch), timeout,
                    )
                    total_affected += result.rows_affected or 0
                await adapter.commit()
            except Exception:
                await adapter.rollback()
                raise

            duration = _time.monotonic() - t0
            self.audit.log(
                connection_id=params.connection_id,
                action="bulk_insert",
                driver=self.pool.get(params.connection_id).driver,
                query=f"INSERT INTO {params.table} ({len(params.rows)} rows)",
                rows_affected=total_affected,
                duration=duration,
            )
            self._record_query(
                params.connection_id,
                f"INSERT INTO {params.table} ({', '.join(params.columns)}) - {len(params.rows)} rows",
                rows_affected=total_affected,
                duration=duration,
            )

            return ActionResult(
                success=True,
                data={
                    "table": params.table,
                    "rows_inserted": total_affected,
                    "columns": params.columns,
                },
            )
        except QueryBlockedError as exc:
            return ActionResult(
                success=False,
                error=f"Blocked by policy ({exc.policy_rule}): {exc.reason}",
            )
        except asyncio.TimeoutError:
            return ActionResult(
                success=False,
                error=f"Bulk insert timed out after {self._get_query_timeout(guard)}s.",
            )
        except KeyError as exc:
            return ActionResult(success=False, error=str(exc))
        except SQLAlchemyError as exc:
            return ActionResult(success=False, error=f"Bulk insert failed: {exc}")

    @action(
        description=(
            "Internal: execute a SELECT query and return rows. Hidden from LLM agents - "
            "the RAG module calls this via the bus, agents should use sql() instead."
        ),
        params_model=FetchResultsParams,
        permissions=["database:read"],
        risk_level="low",
        tags=["query", "read", "internal"],
        internal=True,
    )
    async def fetch_results(self, params: FetchResultsParams) -> ActionResult:
        q_upper = params.query.strip().upper()
        if q_upper and not q_upper.startswith("SELECT") and not q_upper.startswith("WITH"):
            return ActionResult(
                success=False,
                error=(
                    f"fetch_results is for SELECT queries only. "
                    f"This query is a write/DDL operation. "
                    f"Redirecting to database.execute_query for approval."
                ),
                metadata={
                    "requires_approval": True,
                    "tool": "database.execute_query",
                    "redirect": True,
                    "redirect_params": {
                        "connection_id": params.connection_id,
                        "query": params.query,
                        "params": params.params,
                    },
                },
            )

        t0 = _time.monotonic()
        resolved_conn = self._resolve_connection_for_read(params.connection_id)
        guard = self._get_guard(resolved_conn)
        try:
            effective_limit = params.limit
            if guard:
                entry = self.pool.get(resolved_conn)
                if entry.driver not in ("mongodb", "redis"):
                    guard.check_sql_query(params.query)
                effective_limit = guard.get_effective_row_limit(params.limit)

            self._check_transaction_timeout(params.connection_id, guard)

            adapter = self.pool.get_adapter(resolved_conn)
            timeout = self._get_query_timeout(guard)
            result = await self._run_with_timeout(
                adapter.fetch(params.query, params.params, effective_limit), timeout,
            )

            rows = result.rows
            if guard:
                from .security import _extract_table_names
                tables = _extract_table_names(params.query)
                for table in tables:
                    blocked_cols = guard.check_column_access(table, result.columns)
                    if blocked_cols:
                        rows = redact_columns(rows, blocked_cols)

            duration = _time.monotonic() - t0
            self.audit.log(
                connection_id=resolved_conn,
                action="fetch_results",
                driver=self.pool.get(resolved_conn).driver,
                query=params.query,
                rows_affected=len(rows),
                duration=duration,
            )
            self._record_query(
                params.connection_id, params.query,
                rows_returned=len(rows), duration=duration,
            )

            return ActionResult(
                success=True,
                data={
                    "columns": result.columns,
                    "rows": rows,
                    "count": len(rows),
                    "total_count": result.total_count,
                },
            )
        except asyncio.TimeoutError:
            duration = _time.monotonic() - t0
            self.audit.log(
                connection_id=params.connection_id,
                action="fetch_results",
                query=params.query,
                blocked=True,
                block_reason="query_timeout",
                duration=duration,
            )
            self._record_query(
                params.connection_id, params.query,
                success=False, error="query_timeout", duration=duration,
            )
            timeout_val = self._get_query_timeout(guard)
            return ActionResult(
                success=False,
                error=f"Query timed out after {timeout_val}s (max_query_time_seconds).",
            )
        except QueryBlockedError as exc:
            self.audit.log(
                connection_id=params.connection_id,
                action="fetch_results",
                query=params.query,
                blocked=True,
                block_reason=exc.reason,
                duration=_time.monotonic() - t0,
            )
            return ActionResult(
                success=False,
                error=f"Blocked by policy ({exc.policy_rule}): {exc.reason}",
            )
        except KeyError as exc:
            return ActionResult(success=False, error=str(exc))
        except SQLAlchemyError as exc:
            return ActionResult(success=False, error=f"Query failed: {exc}")

    @action(
        description=(
            "Internal: list tables with columns/FK/indexes. Hidden from LLM agents - "
            "called internally by schema() and by the RAG/index modules via the bus."
        ),
        params_model=ListTablesParams,
        permissions=["database:read"],
        risk_level="low",
        tags=["schema", "introspection", "internal"],
        internal=True,
    )
    async def list_tables(self, params: ListTablesParams) -> ActionResult:
        t0 = _time.monotonic()
        try:
            if params.schema_name:
                adapter = self.pool.get_adapter(params.connection_id)
                tables = await adapter.list_tables(params.schema_name)
            else:
                tables = await self._ensure_schema_cached(params.connection_id)

            guard = self._get_guard(params.connection_id)
            if guard:
                tables = filter_table_list(tables, guard)

            self.audit.log(
                connection_id=params.connection_id,
                action="list_tables",
                driver=self.pool.get(params.connection_id).driver,
                duration=_time.monotonic() - t0,
            )

            return ActionResult(
                success=True,
                data={
                    "tables": [_table_to_dict(t) for t in tables],
                    "count": len(tables),
                },
            )
        except KeyError as exc:
            return ActionResult(success=False, error=str(exc))
        except SQLAlchemyError as exc:
            return ActionResult(success=False, error=f"Introspection failed: {exc}")

    @action(
        description=(
            "Internal: full schema introspection. Hidden from LLM agents - "
            "called internally by schema(what='all') and by the RAG module via the bus."
        ),
        params_model=IntrospectParams,
        permissions=["database:read"],
        risk_level="low",
        tags=["schema", "introspection", "internal"],
        internal=True,
    )
    async def introspect(self, params: IntrospectParams) -> ActionResult:
        t0 = _time.monotonic()
        try:
            if params.schema_name:
                adapter = self.pool.get_adapter(params.connection_id)
                schema = await adapter.introspect(params.schema_name)
            else:
                cached_schema = self.schema_cache.get_schema_info(params.connection_id)
                if cached_schema:
                    schema = cached_schema
                else:
                    adapter = self.pool.get_adapter(params.connection_id)
                    schema = await adapter.introspect(params.schema_name)
                    self.schema_cache.store(
                        params.connection_id, tables=schema.tables, schema_info=schema,
                    )

            tables = schema.tables
            guard = self._get_guard(params.connection_id)
            if guard:
                tables = filter_table_list(tables, guard)

            self.audit.log(
                connection_id=params.connection_id,
                action="introspect",
                driver=self.pool.get(params.connection_id).driver,
                duration=_time.monotonic() - t0,
            )

            return ActionResult(
                success=True,
                data={
                    "database": schema.database,
                    "driver": schema.driver,
                    "tables": [_table_to_dict(t) for t in tables],
                    "table_count": len(tables),
                },
            )
        except KeyError as exc:
            return ActionResult(success=False, error=str(exc))
        except SQLAlchemyError as exc:
            return ActionResult(success=False, error=f"Introspection failed: {exc}")

    @action(
        description=(
            "Internal: full table context (schema + sample + stats). Hidden from LLM agents - "
            "called internally by schema(what='describe') and by the RAG module via the bus."
        ),
        params_model=DescribeParams,
        permissions=["database:read"],
        risk_level="low",
        tags=["schema", "introspection", "internal"],
        internal=True,
    )
    async def describe(self, params: DescribeParams) -> ActionResult:
        t0 = _time.monotonic()
        try:
            guard = self._get_guard(params.connection_id)
            if guard:
                guard.check_table_access(params.table)

            adapter = self.pool.get_adapter(params.connection_id)

            await self._ensure_schema_cached(params.connection_id)

            columns = self.schema_cache.get_columns(params.connection_id, params.table)
            if columns is None:
                columns = await adapter.get_columns(params.table)

            stats = self.schema_cache.get_stats(params.connection_id, params.table)
            if stats is None:
                stats = await adapter.table_stats(params.table)
                self.schema_cache.store_stats(params.connection_id, params.table, stats)

            table_info = self.schema_cache.get_table(params.connection_id, params.table)

            sample_rows: list[dict] = []
            sample_columns: list[str] = []
            if params.sample_limit > 0:
                sample = await adapter.sample(params.table, params.sample_limit)
                sample_rows = sample.rows
                sample_columns = sample.columns

                if guard:
                    blocked_cols = guard.check_column_access(params.table, sample_columns)
                    if blocked_cols:
                        sample_rows = redact_columns(sample_rows, blocked_cols)

            table_annotations = self._get_table_annotations(
                params.connection_id, params.table,
            )

            enriched_columns = []
            for c in columns:
                col_dict = asdict(c)
                col_ann = table_annotations.get(c.name)
                if col_ann:
                    col_dict["business_context"] = col_ann
                enriched_columns.append(col_dict)

            result: dict[str, Any] = {
                "table": params.table,
                "row_count": stats.row_count,
                "columns": enriched_columns,
                "column_count": len(columns),
                "foreign_keys": [asdict(fk) for fk in table_info.foreign_keys] if table_info else [],
                "indexes": [asdict(idx) for idx in table_info.indexes] if table_info else [],
                "comment": table_info.comment if table_info else None,
                "sample": {
                    "columns": sample_columns,
                    "rows": sample_rows,
                    "count": len(sample_rows),
                },
            }

            table_ann = table_annotations.get("__table__")
            if table_ann:
                result["business_context"] = table_ann

            self.audit.log(
                connection_id=params.connection_id,
                action="describe",
                driver=self.pool.get(params.connection_id).driver,
                tables=[params.table],
                duration=_time.monotonic() - t0,
            )

            return ActionResult(success=True, data=result)

        except QueryBlockedError as exc:
            self.audit.log(
                connection_id=params.connection_id,
                action="describe",
                blocked=True,
                block_reason=exc.reason,
                duration=_time.monotonic() - t0,
            )
            return ActionResult(
                success=False,
                error=f"Blocked by policy ({exc.policy_rule}): {exc.reason}",
            )
        except KeyError as exc:
            return ActionResult(success=False, error=str(exc))
        except SQLAlchemyError as exc:
            return ActionResult(success=False, error=f"Describe failed: {exc}")

    @action(
        description=(
            "Control an explicit database transaction. Use op='begin' to open, "
            "op='commit' to persist, op='rollback' to undo. All sql() and bulk_insert() "
            "calls on the same connection_id automatically run inside the open transaction."
        ),
        tool_prompt=(
            "Open / commit / rollback an explicit database transaction.\n"
            "\n"
            "## Workflow\n"
            "1. transaction(connection_id='main', op='begin')\n"
            "2. sql(...) / bulk_insert(...) - they all run INSIDE the transaction automatically\n"
            "3a. transaction(connection_id='main', op='commit')   ← persist\n"
            "3b. transaction(connection_id='main', op='rollback') ← undo everything\n"
            "\n"
            "## Rules\n"
            "- Only one open transaction per connection at a time\n"
            "- Forgotten commits auto-rollback after the transaction timeout (default 300s)\n"
            "- A failing sql() inside a transaction does NOT auto-rollback - you decide\n"
            "- On disconnect or session end, an open transaction is rolled back automatically\n"
            "\n"
            "## When to use\n"
            "- Multi-step changes that must succeed or fail together (money transfer, "
            "cascading updates, schema migrations).\n"
            "- Trial changes you want to verify before committing.\n"
        ),
        params_model=TransactionParams,
        permissions=["database:write"],
        risk_level="medium",
        tags=["transaction"],
        cli_label="Tx",
        cli_param="op",
    )
    async def transaction(self, params: TransactionParams) -> ActionResult:
        connection_id = params.connection_id
        op = params.op

        try:
            entry = self.pool.get(connection_id)
        except KeyError as exc:
            return ActionResult(success=False, error=str(exc))

        guard = self._get_guard(connection_id)
        if guard:
            try:
                guard.check_transaction_allowed()
            except QueryBlockedError as exc:
                self.audit.log(
                    connection_id=connection_id,
                    action=f"transaction_{op}",
                    blocked=True,
                    block_reason=exc.reason,
                )
                return ActionResult(
                    success=False,
                    error=f"Blocked by policy ({exc.policy_rule}): {exc.reason}",
                )

        try:
            adapter = self.pool.get_adapter(connection_id)
        except KeyError as exc:
            return ActionResult(success=False, error=str(exc))

        if op == "begin":
            if connection_id in self._tx_start_times:
                return ActionResult(
                    success=False,
                    error=(
                        f"Transaction already open on '{connection_id}'. "
                        f"Commit or rollback first before opening a new one."
                    ),
                )
            try:
                await adapter.begin()
            except SQLAlchemyError as exc:
                return ActionResult(success=False, error=f"Begin failed: {exc}")

            self._tx_start_times[connection_id] = _time.monotonic()
            timeout_info = ""
            if guard:
                max_s = guard.get_transaction_timeout()
                if max_s:
                    timeout_info = f" Timeout: {max_s:.0f}s."

            self.audit.log(
                connection_id=connection_id,
                action="transaction_begin",
                driver=entry.driver,
            )
            self._record_query(connection_id, "BEGIN")
            return ActionResult(
                success=True,
                data={
                    "connection_id": connection_id,
                    "op": "begin",
                    "message": f"Transaction started on '{connection_id}'.{timeout_info}",
                },
            )

        if op == "commit":
            if connection_id not in self._tx_start_times:
                return ActionResult(
                    success=False,
                    error=f"No open transaction on '{connection_id}'. Call op='begin' first.",
                )
            try:
                self._check_transaction_timeout(connection_id, guard)
                await adapter.commit()
            except QueryBlockedError as exc:
                # Timeout exceeded - rollback to release locks before reporting.
                try:
                    await adapter.rollback()
                except Exception as exc:
                    logger.debug("module best-effort block failed: %s", exc)
                self._tx_start_times.pop(connection_id, None)
                return ActionResult(
                    success=False,
                    error=(
                        f"Transaction timed out and was rolled back: {exc.reason}"
                    ),
                )
            except SQLAlchemyError as exc:
                return ActionResult(success=False, error=f"Commit failed: {exc}")

            self._tx_start_times.pop(connection_id, None)
            self.audit.log(
                connection_id=connection_id,
                action="transaction_commit",
                driver=entry.driver,
            )
            self._record_query(connection_id, "COMMIT")
            return ActionResult(
                success=True,
                data={
                    "connection_id": connection_id,
                    "op": "commit",
                    "message": f"Transaction committed on '{connection_id}'.",
                },
            )

        # op == "rollback"
        if connection_id not in self._tx_start_times:
            return ActionResult(
                success=False,
                error=f"No open transaction on '{connection_id}'. Call op='begin' first.",
            )
        try:
            await adapter.rollback()
        except SQLAlchemyError as exc:
            self._tx_start_times.pop(connection_id, None)
            return ActionResult(success=False, error=f"Rollback failed: {exc}")

        self._tx_start_times.pop(connection_id, None)
        self.audit.log(
            connection_id=connection_id,
            action="transaction_rollback",
            driver=entry.driver,
        )
        self._record_query(connection_id, "ROLLBACK")
        return ActionResult(
            success=True,
            data={
                "connection_id": connection_id,
                "op": "rollback",
                "message": f"Transaction rolled back on '{connection_id}'.",
            },
        )

    @action(
        description=(
            "Internal: extract schema as IndexEntry + Relation data for the index module. "
            "Hidden from LLM agents - called automatically by index.scan via the service bus."
        ),
        params_model=ExtractForIndexParams,
        permissions=["database:read"],
        risk_level="low",
        tags=["index", "extraction", "internal"],
        internal=True,
    )
    async def extract_for_index(self, params: ExtractForIndexParams) -> ActionResult:
        connection_id = params.root
        if connection_id not in self.pool:
            return ActionResult(
                success=False,
                error=f"Connection '{connection_id}' not found. Connect first.",
            )

        try:
            adapter = self.pool.get_adapter(connection_id)
            tables = await adapter.list_tables()
        except SQLAlchemyError as exc:
            return ActionResult(success=False, error=f"Introspection failed: {exc}")

        conn_annotations = self._annotations.get(connection_id, {})

        entries, relations = _build_index_entries(
            params.source_id, tables, conn_annotations,
        )

        for entry in entries:
            if entry["kind"] == "table":
                try:
                    stats = await adapter.table_stats(entry["name"])
                    entry["metadata"]["row_count"] = stats.row_count
                except SQLAlchemyError:
                    logger.debug("table_stats_failed table=%s", entry["name"], exc_info=True)

        return ActionResult(
            success=True,
            data={
                "entries": entries,
                "relations": relations,
            },
        )

    def _get_table_annotations(
        self, connection_id: str, table: str,
    ) -> dict[str, Any]:
        return self._annotations.get(connection_id, {}).get(table, {})

    @action(
        description=(
            "Universal SQL execution. Auto-detects query type: SELECT/WITH/SHOW/EXPLAIN/PRAGMA "
            "return rows; INSERT/UPDATE/DELETE return affected row count; CREATE/ALTER/DROP "
            "return success."
        ),
        tool_prompt=(
            "Execute any SQL query on a configured database connection.\n"
            "\n"
            "## When to use sql() vs the other actions\n"
            "- sql() is the default for any query you can write yourself\n"
            "- Use schema() instead of sql('SELECT name FROM sqlite_master ...')\n"
            "- Use bulk_insert() instead of sql() when inserting >50 rows - much faster\n"
            "- Use browse() instead of sql() for paginated table previews\n"
            "- Use search_data() instead of sql() for simple column lookups\n"
            "- Wrap multi-step changes in transaction(op='begin')…transaction(op='commit')\n"
            "\n"
            "## Query types\n"
            "- SELECT / WITH / SHOW / EXPLAIN / PRAGMA → returns rows as a list of dicts\n"
            "- INSERT / UPDATE / DELETE                → returns rows_affected\n"
            "- CREATE / ALTER / DROP                   → returns success\n"
            "\n"
            "## Parameters (NEVER interpolate user input)\n"
            "  sql(query='SELECT * FROM users WHERE id = :p0', params=[42])\n"
            "  sql(query='UPDATE users SET active=:p0 WHERE id=:p1', params=[False, 42])\n"
            "\n"
            "## Tips\n"
            "- ALWAYS add LIMIT to SELECT queries (default cap: 100 rows)\n"
            "- Use sql('EXPLAIN <query>') to check the plan before slow queries\n"
            "- Inside a transaction, sql() automatically runs in the open transaction\n"
        ),
        risk_level="medium",
        tags=["database", "sql"],
        aliases=["query", "requete"],
        params_model=SqlParams,
        cli_label="SQL",
        cli_param="query",
    )
    async def sql(self, params: Any) -> ActionResult:
        """Universal SQL execution - SELECT returns rows, DML returns affected count."""
        query = (params.query if hasattr(params, "query") else params.get("query", "")).strip()
        connection_id = params.connection_id if hasattr(params, "connection_id") else params.get("connection_id", "default")
        sql_params = getattr(params, "params", None) or (params.get("params") if isinstance(params, dict) else None)

        if not query:
            return ActionResult(success=False, error="Missing 'query' parameter. Example: sql(query=\"SELECT * FROM users LIMIT 10\")")

        entry = self.pool.get(connection_id)
        if entry is None:
            available = list(self.pool._connections.keys())
            return ActionResult(
                success=False,
                error=f"No connection '{connection_id}'. Use connect() first. Active connections: {available or '(none)'}",
            )

        # Basic SQL validation - catch common mistakes before hitting the database
        validation_error = self._validate_sql(query)
        if validation_error:
            return ActionResult(success=False, error=validation_error)

        q_upper = query.upper().lstrip()
        is_select = q_upper.startswith("SELECT") or q_upper.startswith("WITH") or q_upper.startswith("SHOW") or q_upper.startswith("DESCRIBE") or q_upper.startswith("EXPLAIN") or q_upper.startswith("PRAGMA")

        t0 = _time.monotonic()
        try:
            if is_select:
                from digitorn.modules.database.params import FetchResultsParams
                fr_params = FetchResultsParams(connection_id=connection_id, query=query, params=sql_params, limit=100)
                result = await self.fetch_results(fr_params)
                elapsed = _time.monotonic() - t0
                if not result.success:
                    return ActionResult(success=False, error=self._enhance_sql_error(result.error, query))
                data = result.data if isinstance(result.data, dict) else {}
                data["type"] = "select"
                data["elapsed_ms"] = round(elapsed * 1000)
                data["query"] = query
                return ActionResult(success=True, data=data)
            else:
                from digitorn.modules.database.params import ExecuteQueryParams
                eq_params = ExecuteQueryParams(connection_id=connection_id, query=query, params=sql_params)
                result = await self.execute_query(eq_params)
                elapsed = _time.monotonic() - t0
                if not result.success:
                    return ActionResult(success=False, error=self._enhance_sql_error(result.error, query))
                data = result.data if isinstance(result.data, dict) else {}
                data["type"] = "execute"
                data["elapsed_ms"] = round(elapsed * 1000)
                data["query"] = query
                return ActionResult(success=True, data=data)
        except Exception as exc:
            return ActionResult(success=False, error=self._enhance_sql_error(str(exc), query))

    @staticmethod
    def _validate_sql(query: str) -> str | None:
        q = query.strip()
        if not q:
            return "Empty query"
        # Detect common LLM mistakes
        if q.endswith("\\"):
            return "Query ends with backslash - likely a formatting error. Remove the trailing \\"
        # Unbalanced quotes
        if q.count("'") % 2 != 0:
            return f"Unbalanced single quotes in query. Found {q.count(chr(39))} - should be even."
        if q.count('"') % 2 != 0:
            return f"Unbalanced double quotes in query. Found {q.count(chr(34))} - should be even."
        # Unbalanced parentheses
        if q.count("(") != q.count(")"):
            return f"Unbalanced parentheses: {q.count('(')} open vs {q.count(')')} close."
        return None

    @staticmethod
    def _enhance_sql_error(error: str | None, query: str) -> str:
        if not error:
            return "Unknown SQL error"
        err = str(error)
        hints = []
        err_lower = err.lower()
        if "no such table" in err_lower or "relation" in err_lower and "does not exist" in err_lower:
            hints.append("Use schema(what='tables') to see available tables.")
        if "no such column" in err_lower or "column" in err_lower and "does not exist" in err_lower:
            hints.append("Use schema(what='describe', table='...') to see column names.")
        if "syntax error" in err_lower:
            hints.append("Check SQL syntax. Common issues: missing commas, wrong keywords, unbalanced quotes.")
        if "unique constraint" in err_lower or "duplicate" in err_lower:
            hints.append("A row with this key already exists. Use UPDATE to modify or add ON CONFLICT clause.")
        if "foreign key" in err_lower:
            hints.append("Foreign key violation. The referenced row must exist in the parent table.")
        if hints:
            return f"{err}\n\nHints:\n" + "\n".join(f"  - {h}" for h in hints)
        return err

    @action(
        description=(
            "Explore database schema. Use what='tables' to list, what='describe' for "
            "one table in detail (columns, types, FK, indexes, sample rows), what='all' "
            "for the full dump."
        ),
        tool_prompt=(
            "Explore the schema of a connected database.\n"
            "\n"
            "## Modes\n"
            "- schema(what='tables') - list all tables (START HERE for unfamiliar databases)\n"
            "- schema(what='describe', table='users') - full detail on one table:\n"
            "  columns + types + nullability + primary key + foreign keys + indexes + sample rows\n"
            "- schema(what='all') - full schema dump for every table (use sparingly on large DBs)\n"
            "\n"
            "## Workflow\n"
            "1. schema(what='tables') → see what's available\n"
            "2. schema(what='describe', table='<one>') → understand the table you'll query\n"
            "3. relations(table='<one>') → see how it joins to other tables\n"
            "4. sql(query='SELECT ...') → write the actual query\n"
        ),
        risk_level="low",
        tags=["database", "schema", "explore"],
        aliases=["db_schema", "tables", "structure"],
        params_model=SchemaParams,
        cli_label="Schema",
        cli_param="what",
    )
    async def schema(self, params: Any) -> ActionResult:
        """Unified schema exploration - wraps list_tables / describe / introspect."""
        what = params.what if hasattr(params, "what") else params.get("what", "tables")
        connection_id = params.connection_id if hasattr(params, "connection_id") else params.get("connection_id", "default")
        table = params.table if hasattr(params, "table") else params.get("table")

        try:
            if what == "describe":
                if not table:
                    return ActionResult(success=False, error="'table' is required when what='describe'. Example: schema(what='describe', table='users')")
                from digitorn.modules.database.params import DescribeParams
                return await self.describe(DescribeParams(connection_id=connection_id, table=table, sample_limit=5))

            elif what == "all":
                from digitorn.modules.database.params import IntrospectParams
                return await self.introspect(IntrospectParams(connection_id=connection_id))

            else:  # "tables" (default)
                from digitorn.modules.database.params import ListTablesParams
                return await self.list_tables(ListTablesParams(connection_id=connection_id))

        except Exception as exc:
            return ActionResult(success=False, error=f"Schema exploration failed: {exc}")

    @action(
        description=(
            "Browse a table interactively with pagination. Like scrolling through "
            "a spreadsheet. Shows rows with column names. "
            "Example: browse(table=\"users\", page=1, per_page=20)"
        ),
        risk_level="low",
        tags=["database", "browse", "explore"],
        aliases=["parcourir", "navigate", "scroll"],
        params_model=BrowseParams,
        cli_label="Browse",
        cli_param="table",
    )
    async def browse(self, params: Any) -> ActionResult:
        table = params.table if hasattr(params, "table") else params.get("table", "")
        connection_id = params.connection_id if hasattr(params, "connection_id") else params.get("connection_id", "default")
        page = params.page if hasattr(params, "page") else params.get("page", 1)
        per_page = min(params.per_page if hasattr(params, "per_page") else params.get("per_page", 20), 100)

        if not table:
            return ActionResult(success=False, error="Missing 'table' parameter")

        # Validate table name to prevent SQL injection (it's interpolated below)
        try:
            validate_sql_identifier(table, "table name")
        except (ValueError, Exception) as exc:
            return ActionResult(success=False, error=f"Invalid table name: {exc}")

        entry = self.pool.get(connection_id)
        if entry is None:
            return ActionResult(success=False, error=f"No connection '{connection_id}'.")

        offset = (page - 1) * per_page
        try:
            from digitorn.modules.database.params import FetchResultsParams

            # Get total count
            count_result = await self.fetch_results(FetchResultsParams(
                connection_id=connection_id, query=f"SELECT COUNT(*) AS total FROM {table}", limit=1,
            ))
            total = 0
            if count_result.success and isinstance(count_result.data, dict):
                count_rows = count_result.data.get("rows", [])
                if count_rows:
                    row = count_rows[0]
                    total = row.get("total", row[0] if isinstance(row, (list, tuple)) else 0) if isinstance(row, dict) else row[0]

            # Get page data
            page_result = await self.fetch_results(FetchResultsParams(
                connection_id=connection_id,
                query=f"SELECT * FROM {table} LIMIT {per_page} OFFSET {offset}",
                limit=per_page,
            ))
            if not page_result.success:
                return ActionResult(success=False, error=page_result.error)

            data = page_result.data if isinstance(page_result.data, dict) else {}
            total_pages = (total + per_page - 1) // per_page if total else 1

            return ActionResult(success=True, data={
                "table": table,
                "columns": data.get("columns", []),
                "rows": data.get("rows", []),
                "page": page,
                "per_page": per_page,
                "total_rows": total,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1,
            })
        except Exception as exc:
            return ActionResult(success=False, error=f"Browse error: {exc}")

    @action(
        description=(
            "Show foreign key relationships for a table. Reveals how tables "
            "are connected - essential for writing JOINs. "
            "Example: relations(table=\"orders\")"
        ),
        risk_level="low",
        tags=["database", "schema", "relations"],
        aliases=["fk", "foreign_keys", "joinable"],
        params_model=RelationsParams,
        cli_label="Relations",
        cli_param="table",
    )
    async def relations(self, params: Any) -> ActionResult:
        table = params.table if hasattr(params, "table") else params.get("table", "")
        connection_id = params.connection_id if hasattr(params, "connection_id") else params.get("connection_id", "default")

        if not table:
            return ActionResult(success=False, error="Missing 'table' parameter")

        entry = self.pool.get(connection_id)
        if entry is None:
            return ActionResult(success=False, error=f"No connection '{connection_id}'.")

        try:
            # Use SQLAlchemy reflection for FK discovery
            from sqlalchemy import inspect as sa_inspect
            inspector = sa_inspect(entry.adapter._engine)
            fks = inspector.get_foreign_keys(table)
            incoming: list[dict[str, Any]] = []

            # Also find tables that reference this table
            all_tables = inspector.get_table_names()
            for other_table in all_tables:
                if other_table == table:
                    continue
                other_fks = inspector.get_foreign_keys(other_table)
                for fk in other_fks:
                    if fk.get("referred_table") == table:
                        incoming.append({
                            "from_table": other_table,
                            "from_columns": fk.get("constrained_columns", []),
                            "to_columns": fk.get("referred_columns", []),
                        })

            outgoing = [{
                "to_table": fk.get("referred_table"),
                "from_columns": fk.get("constrained_columns", []),
                "to_columns": fk.get("referred_columns", []),
                "name": fk.get("name", ""),
            } for fk in fks]

            return ActionResult(success=True, data={
                "table": table,
                "outgoing": outgoing,
                "incoming": incoming,
                "total_relations": len(outgoing) + len(incoming),
            })
        except Exception as exc:
            return ActionResult(success=False, error=f"Relations error: {exc}")

    @action(
        description=(
            "Search for data in a table by column value. Supports exact match "
            "and partial match (LIKE). Like Ctrl+F in a spreadsheet. "
            "Example: search_data(table=\"users\", column=\"email\", value=\"@gmail.com\", mode=\"contains\")"
        ),
        risk_level="low",
        tags=["database", "search", "filter"],
        aliases=["chercher_donnees", "find_data", "filter"],
        params_model=SearchDataParams,
        cli_label="Search",
        cli_param="table",
    )
    async def search_data(self, params: Any) -> ActionResult:
        table = params.table if hasattr(params, "table") else params.get("table", "")
        column = params.column if hasattr(params, "column") else params.get("column", "")
        value = params.value if hasattr(params, "value") else params.get("value", "")
        connection_id = params.connection_id if hasattr(params, "connection_id") else params.get("connection_id", "default")
        mode = params.mode if hasattr(params, "mode") else params.get("mode", "contains")
        limit = min(params.limit if hasattr(params, "limit") else params.get("limit", 20), 100)

        if not table or not column:
            return ActionResult(success=False, error="Missing 'table' and 'column' parameters")

        # Validate table and column to prevent SQL injection (interpolated below)
        try:
            validate_sql_identifier(table, "table name")
            validate_sql_identifier(column, "column name")
        except (ValueError, Exception) as exc:
            return ActionResult(success=False, error=f"Invalid identifier: {exc}")

        entry = self.pool.get(connection_id)
        if entry is None:
            return ActionResult(success=False, error=f"No connection '{connection_id}'.")

        # Escape value for SQL safety
        safe_value = value.replace("'", "''")

        if mode == "exact":
            where = f"{column} = '{safe_value}'"
        elif mode == "starts_with":
            where = f"{column} LIKE '{safe_value}%'"
        elif mode == "ends_with":
            where = f"{column} LIKE '%{safe_value}'"
        else:  # contains
            where = f"{column} LIKE '%{safe_value}%'"

        try:
            from digitorn.modules.database.params import FetchResultsParams

            query = f"SELECT * FROM {table} WHERE {where} LIMIT {limit}"
            result = await self.fetch_results(FetchResultsParams(
                connection_id=connection_id, query=query, limit=limit,
            ))
            if not result.success:
                return ActionResult(success=False, error=result.error)

            data = result.data if isinstance(result.data, dict) else {}
            return ActionResult(success=True, data={
                "table": table,
                "column": column,
                "value": value,
                "mode": mode,
                "columns": data.get("columns", []),
                "rows": data.get("rows", []),
                "count": data.get("count", 0),
                "truncated": data.get("count", 0) >= limit,
            })
        except Exception as exc:
            return ActionResult(success=False, error=f"Search error: {exc}")

    async def health_check(self) -> dict[str, Any]:
        connections = self.pool.list_connections()
        healthy = all(c["connected"] for c in connections)
        return {
            "status": "ok" if healthy or not connections else "error",
            "module_id": self.MODULE_ID,
            "version": self.VERSION,
            "connections": connections,
            "connection_count": len(connections),
        }

    def get_context_snippet(self) -> str | None:
        """Complementary context for database module."""
        connections = self.pool.list_connections()
        if not connections:
            return None
        ids = ", ".join(c["connection_id"] for c in connections)
        return (
            f"Database connections ready: {ids}. "
            "Use connection_id in all queries. "
            "Tables may require schema prefix (e.g. ecommerce.customers)."
        )

    def metrics(self) -> dict[str, Any]:
        return {
            "connection_count": len(self.pool),
            "connections": self.pool.list_connections(),
        }

    def get_manifest(self) -> ModuleManifest:
        return ModuleManifest.from_module(self).model_copy(update={
            "description": (
                "Multi-driver async database module. Supports PostgreSQL, MySQL, "
                "SQLite, MSSQL, and Oracle via SQLAlchemy. Provides named connections, "
                "full schema introspection, parameterized queries, transaction control, "
                "EXPLAIN query plans, cursor-based pagination, schema diff, query history, "
                "health monitoring with auto-reconnect, read replica routing, "
                "and watcher integration for change detection."
            ),
            "author": "Digitorn Team",
            "tags": ["core", "database", "sql", "introspection"],
            "declared_permissions": [
                "database:read",
                "database:write",
                "database:admin",
            ],
            "provides_services": ["database"],
            "supported_constraints": self.CONSTRAINTS,
        })

def _table_to_dict(table: Any) -> dict[str, Any]:
    return {
        "name": table.name,
        "schema": table.schema,
        "columns": [asdict(c) for c in table.columns],
        "foreign_keys": [asdict(fk) for fk in table.foreign_keys],
        "indexes": [asdict(idx) for idx in table.indexes],
        "comment": table.comment,
    }

def _build_index_entries(
    source_id: str,
    tables: list[Any],
    annotations: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    import hashlib

    annotations = annotations or {}
    entries: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []

    for table in tables:
        table_path = f"{table.schema}.{table.name}" if table.schema else table.name
        table_id = _make_entry_id(source_id, table_path, table.name)
        table_ann = annotations.get(table.name, {})
        table_level_ann = table_ann.get("__table__", {})

        col_sigs = ", ".join(
            f"{c.name} {c.type}{'  PK' if c.primary_key else ''}"
            for c in table.columns
        )
        signature = f"TABLE {table.name} ({col_sigs})"

        summary = table_level_ann.get("description", "") or table.comment or ""

        schema_repr = "|".join(f"{c.name}:{c.type}:{c.nullable}" for c in table.columns)
        content_hash = hashlib.sha256(schema_repr.encode()).hexdigest()[:16]

        metadata: dict[str, Any] = {
            "column_count": len(table.columns),
            "index_count": len(table.indexes),
            "fk_count": len(table.foreign_keys),
        }
        if table_level_ann.get("tags"):
            metadata["tags"] = table_level_ann["tags"]
        if table_level_ann.get("rules"):
            metadata["rules"] = table_level_ann["rules"]
        if table_level_ann.get("glossary"):
            metadata["glossary"] = table_level_ann["glossary"]

        entries.append({
            "entry_id": table_id,
            "source_id": source_id,
            "path": table_path,
            "kind": "table",
            "name": table.name,
            "signature": signature,
            "summary": summary,
            "content_hash": content_hash,
            "metadata": metadata,
        })

        for col in table.columns:
            col_id = _make_entry_id(source_id, table_path, f"{table.name}.{col.name}")
            col_sig = f"{col.name} {col.type}"
            if col.primary_key:
                col_sig += " PRIMARY KEY"
            if not col.nullable:
                col_sig += " NOT NULL"
            if col.default:
                col_sig += f" DEFAULT {col.default}"

            col_ann = table_ann.get(col.name, {})
            col_summary = col_ann.get("description", "") or col.comment or ""
            col_meta: dict[str, Any] = {"table": table.name}
            if col_ann.get("tags"):
                col_meta["tags"] = col_ann["tags"]

            entries.append({
                "entry_id": col_id,
                "source_id": source_id,
                "path": table_path,
                "kind": "column",
                "name": f"{table.name}.{col.name}",
                "signature": col_sig,
                "summary": col_summary,
                "content_hash": content_hash,
                "metadata": col_meta,
            })
            relations.append({
                "from_id": table_id,
                "to_id": col_id,
                "kind": "contains",
            })

        for fk in table.foreign_keys:
            ref_table_id = _make_entry_id(source_id, fk.referred_table, fk.referred_table)
            relations.append({
                "from_id": table_id,
                "to_id": ref_table_id,
                "kind": "references",
                "metadata": {
                    "column": fk.column,
                    "referred_table": fk.referred_table,
                    "referred_column": fk.referred_column,
                },
            })

    return entries, relations

def _make_entry_id(source_id: str, path: str, name: str) -> str:
    import hashlib
    raw = f"{source_id}:{path}:{name}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def _resolve_secret(env_var: str | None) -> str | None:
    if not env_var:
        return None
    return os.environ.get(env_var)

def _resolve_policy(data: dict[str, Any]) -> DatabasePolicy:
    preset = data.get("preset")
    if preset:
        presets = {
            "readonly": DatabasePolicy.readonly,
            "safe_write": DatabasePolicy.safe_write,
            "unrestricted": DatabasePolicy.unrestricted,
        }
        factory = presets.get(preset)
        if not factory:
            raise ValueError(
                f"Unknown policy preset '{preset}'. "
                f"Available: {', '.join(presets)}",
            )
        return factory()
    return DatabasePolicy(**{k: v for k, v in data.items() if v is not None})

