"""Digitorn - Database engine and session management.

Provides a single async SQLAlchemy engine and session factory used by
the entire daemon. Supports any SQLAlchemy-compatible backend (SQLite,
PostgreSQL, MySQL, etc.) - the URL in config determines which one.

Usage::

    from digitorn.core.database import get_session, init_db

    await init_db(settings)

    async with get_session() as session:
        result = await session.execute(select(MyModel))
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
from typing import Any, AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from digitorn.core.config import Settings


def _is_asyncpg_teardown_noise(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    # Type check `CancelledError` directly; `str(exc)` is empty.
    import asyncio as _asyncio
    if isinstance(exc, _asyncio.CancelledError):
        return True
    s = str(exc)
    return (
        "attached to a different loop" in s
        or "unknown protocol state" in s
        or "InternalClientError" in s
        # Shutdown-time futures harmless: the daemon resumes from DB.
        or "cannot schedule new futures after shutdown" in s
        or "Event loop is closed" in s
    )


class _SuppressAsyncpgTeardownFilter(logging.Filter):
    """Silences `Exception terminating connection` tracebacks from the
    SQLA pool when the underlying error is the known-harmless asyncpg
    cross-loop close race. Attached directly to the pool instance's
    per-instance logger (`sqlalchemy.pool.impl.<Pool>.0xXXXX`), which
    is where SA actually emits the record, so there's no propagation /
    ancestor-filter issue.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "Exception terminating connection" in msg or \
                "Exception closing connection" in msg:
            exc = record.exc_info
            if exc and _is_asyncpg_teardown_noise(exc[1]):
                return False
        return True


def _install_pool_logger_filter(engine: AsyncEngine) -> None:
    """Attach the suppression filter to this engine's pool logger.

    SA creates a per-pool logger via `instance_logger`; the name is
    predictable once the pool exists (`sqlalchemy.pool.impl.<Class>.0xXXXX`)
    but we don't have to construct it - the pool object exposes it
    directly as `pool.logger`.
    """
    try:
        sync_engine = engine.sync_engine
        pool = sync_engine.pool
        pool_logger = getattr(pool, "logger", None)
        if pool_logger is None:
            return
        # Idempotent: don't stack duplicate filters if init_db ever runs twice.
        existing = [
            f for f in pool_logger.filters
            if isinstance(f, _SuppressAsyncpgTeardownFilter)
        ]
        if not existing:
            pool_logger.addFilter(_SuppressAsyncpgTeardownFilter())
    except Exception:
        # Filter install is best-effort - its absence only affects log
        # noise, never correctness.
        pass


def _install_loop_exception_handler() -> None:
    """Mute the `Future exception was never retrieved` twin that
    asyncio prints when SA's shielded teardown future completes with
    the same asyncpg error. Wraps `new_event_loop` so freshly-created
    loops (worker_pool, tests) inherit the handler too.
    """
    import asyncio as _asyncio

    def _handler_factory(
        prev: Any,
    ) -> Any:
        def _handler(
            lp: _asyncio.AbstractEventLoop, ctx: dict[str, Any],
        ) -> None:
            exc = ctx.get("exception")
            msg = ctx.get("message", "")
            if _is_asyncpg_teardown_noise(exc):
                return
            if (
                "Future exception was never retrieved" in msg
                and _is_asyncpg_teardown_noise(exc)
            ):
                return
            if prev is not None:
                prev(lp, ctx)
            else:
                lp.default_exception_handler(ctx)
        return _handler

    def _install_on(target_loop: _asyncio.AbstractEventLoop) -> None:
        try:
            prev = target_loop.get_exception_handler()
            target_loop.set_exception_handler(_handler_factory(prev))
        except Exception as exc:
            logger.debug("database best-effort block failed: %s", exc)

    try:
        _install_on(_asyncio.get_event_loop())
    except RuntimeError:
        pass

    if getattr(_asyncio, "_digitorn_newloop_patched", False):
        return
    _asyncio._digitorn_newloop_patched = True  # type: ignore[attr-defined]
    _orig_new_loop = _asyncio.new_event_loop

    def _patched_new_loop() -> _asyncio.AbstractEventLoop:
        lp = _orig_new_loop()
        _install_on(lp)
        return lp

    _asyncio.new_event_loop = _patched_new_loop  # type: ignore[assignment]


_install_loop_exception_handler()


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Per-coroutine override of the session factory. The PersistWorker
# (a thread with its own asyncio loop and its own engine + pool)
# sets this contextvar before running each persist job so all DB
# code inside the worker uses connections bound to the worker loop.
# Default `None` means "use the module-level _session_factory" -
# i.e. the daemon's main engine - so nothing changes for code paths
# that aren't routed through the worker.
from contextvars import ContextVar as _ContextVar
_session_factory_override: _ContextVar[
    "async_sessionmaker[AsyncSession] | None"
] = _ContextVar("digitorn_session_factory_override", default=None)


def set_session_factory_override(
    factory: "async_sessionmaker[AsyncSession] | None",
) -> Any:
    """Push a session-factory override onto the current async context.

    Returns the contextvars Token so the caller can `reset()` later.
    Used exclusively by `runtime/persist_worker.py`; everywhere else
    relies on the default (module-level factory).
    """
    return _session_factory_override.set(factory)


def reset_session_factory_override(token: Any) -> None:
    """Pop the override pushed by `set_session_factory_override`."""
    try:
        _session_factory_override.reset(token)
    except (LookupError, ValueError):
        # Token expired or wasn't created by us - safe to ignore.
        pass


async def init_db(settings: Settings) -> AsyncEngine:
    """Create the async engine and session factory.

    Called once at daemon startup. Creates all tables if they don't exist.
    """
    import digitorn.core.models  # noqa: F401 - register ORM models with Base.metadata

    global _engine, _session_factory

    is_sqlite = settings.database.url.startswith("sqlite")
    is_asyncpg = "+asyncpg" in settings.database.url
    db_url = settings.database.url
    connect_args: dict[str, Any] = {}
    if is_sqlite:
        connect_args["check_same_thread"] = False
    if is_asyncpg:
        # Disable the asyncpg prepared-statement cache for PgBouncer /
        # Neon transaction-pool compatibility (cached handles point at
        # the wrong session after the pooler rotates).
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0
        # 30 s command timeout: Neon cold-starts can take 10 s+.
        connect_args["command_timeout"] = 30.0

    pool_kwargs: dict[str, Any] = {}
    if is_sqlite:
        pool_kwargs["pool_pre_ping"] = True
    elif is_asyncpg:
        # `pool_pre_ping` stays off: SA's `SELECT 1` would call
        # `ssl.write` synchronously and stall the loop.
        pool_kwargs.update(
            pool_size=5, max_overflow=10, pool_timeout=30,
            pool_recycle=300, pool_pre_ping=False,
        )
    else:
        pool_kwargs["pool_pre_ping"] = True
        pool_kwargs.update(pool_size=50, max_overflow=100, pool_timeout=30)
        pool_kwargs["pool_recycle"] = 300

    _engine = create_async_engine(
        db_url,
        echo=settings.database.echo,
        connect_args=connect_args,
        **pool_kwargs,
    )

    # Silence the cosmetic cross-loop asyncpg teardown traceback.
    if is_asyncpg:
        _install_pool_logger_filter(_engine)

    # WAL + busy_timeout so concurrent SQLite writers don't get
    # `database is locked`.
    if is_sqlite:
        from sqlalchemy import event as _sa_event

        sync_engine = _engine.sync_engine

        @_sa_event.listens_for(sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                # `synchronous=FULL` fsyncs every commit so no
                # committed transaction is lost on crash.
                cur.execute("PRAGMA synchronous=FULL")
                # 30 s busy timeout absorbs bursts of parallel writes.
                cur.execute("PRAGMA busy_timeout=30000")
                # `PRAGMA foreign_keys=ON` left off: it exposes a
                # pre-existing `users -> applications` FK mismatch.
            finally:
                cur.close()

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Retry-with-backoff to ride out serverless-Postgres cold starts
    # (Neon, Supabase) that surface as `OSError [WinError 121]`,
    # `ConnectionDoesNotExistError` or `CannotConnectNowError`.
    import asyncio as _asyncio
    _init_log = logging.getLogger(__name__)
    _MAX_INIT_RETRIES = 4
    last_exc: BaseException | None = None
    for attempt in range(_MAX_INIT_RETRIES):
        try:
            async with _engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.run_sync(_migrate_missing_columns)
                await conn.run_sync(_migrate_installed_packages_unique_constraint)
                await conn.run_sync(_migrate_applications_drop_app_id_unique)
                await conn.run_sync(_migrate_history_log_seq_unique)
                # `create_all` covers the `ts` unique index on fresh
                # DBs; the explicit migration adds the seq uniqueness to
                # existing DBs.
            break
        except (OSError, ConnectionError, _asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt == _MAX_INIT_RETRIES - 1:
                raise
            backoff_s = 2.0 * (2 ** attempt)
            _init_log.warning(
                "init_db_retry attempt=%d/%d backoff=%.1fs reason=%s",
                attempt + 1, _MAX_INIT_RETRIES, backoff_s, exc,
            )
            await _asyncio.sleep(backoff_s)
        except Exception as exc:
            # Inspect the type name + cause chain so we don't import the
            # asyncpg wrapper classes eagerly.
            text_repr = f"{type(exc).__name__}: {exc}"
            cause = getattr(exc, "__cause__", None)
            cause_text = f"{type(cause).__name__}: {cause}" if cause else ""
            transient_markers = (
                "ConnectionDoesNotExist",
                "CannotConnectNow",
                "ConnectionResetError",
                "WinError 121",
                "semaphore timeout",
                "Connection refused",
                "Connection lost",
                "starting up",
            )
            is_transient = any(
                m in text_repr or m in cause_text for m in transient_markers
            )
            if not is_transient or attempt == _MAX_INIT_RETRIES - 1:
                raise
            last_exc = exc
            backoff_s = 2.0 * (2 ** attempt)
            _init_log.warning(
                "init_db_retry_transient attempt=%d/%d backoff=%.1fs reason=%s cause=%s",
                attempt + 1, _MAX_INIT_RETRIES, backoff_s, text_repr, cause_text,
            )
            await _asyncio.sleep(backoff_s)
    else:
        # Loop exhausted without break - shouldn't happen because the
        # last attempt re-raises, but defensive.
        if last_exc is not None:
            raise last_exc

    return _engine


def _default_for_type(col_type: str) -> str:
    """Return a safe DEFAULT clause for a NOT NULL column without server_default."""
    upper = col_type.upper()
    if any(t in upper for t in ("INT", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL", "REAL")):
        return " DEFAULT 0"
    if "BOOL" in upper:
        return " DEFAULT FALSE"
    if any(t in upper for t in ("JSON", "JSONB")):
        return " DEFAULT '{}'"
    return " DEFAULT ''"


def _migrate_missing_columns(conn) -> None:
    """Add missing columns to existing tables.

    SQLAlchemy's create_all only creates missing tables, not columns.
    This runs ALTER TABLE ADD COLUMN for any columns that don't exist yet.
    Safe to call repeatedly (skips columns that already exist).
    """
    from sqlalchemy import Boolean, inspect, text

    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name not in existing:
                col_type = col.type.compile(conn.dialect)
                nullable = "NULL" if col.nullable else "NOT NULL"
                default = ""
                if col.server_default is not None:
                    raw_arg = col.server_default.arg
                    # server_default.arg can be a raw string, a TextClause,
                    # or (rarely) a ColumnDefault. Coerce to a bare SQL
                    # literal we can paste into the ALTER TABLE.
                    if hasattr(raw_arg, "text"):  # SQLAlchemy TextClause
                        raw = raw_arg.text
                    else:
                        raw = str(raw_arg)
                    raw = raw.strip()
                    if raw == "":
                        default = " DEFAULT ''"
                    elif raw.startswith("'") and raw.endswith("'"):
                        # Already SQL-quoted.
                        default = f" DEFAULT {raw}"
                    elif raw.lstrip("-").replace(".", "", 1).isdigit():
                        # Postgres rejects 0/1 as a boolean default.
                        if isinstance(col.type, Boolean):
                            default = " DEFAULT FALSE" if raw in ("0", "0.0") else " DEFAULT TRUE"
                        else:
                            default = f" DEFAULT {raw}"
                    elif raw.upper() in ("TRUE", "FALSE", "NULL", "CURRENT_TIMESTAMP"):
                        # SQL keyword literal.
                        default = f" DEFAULT {raw.upper()}"
                    else:
                        # String literal - SQL-escape embedded single quotes.
                        escaped = raw.replace("'", "''")
                        default = f" DEFAULT '{escaped}'"
                elif not col.nullable:
                    default = _default_for_type(col_type)
                sql = (
                    f"ALTER TABLE {table.name} ADD COLUMN "
                    f"{col.name} {col_type} {nullable}{default}"
                )
                try:
                    conn.execute(text(sql))
                except Exception as exc:
                    # Surface the exact SQL that broke so the operator can
                    # see it immediately (instead of `sqlite3.OperationalError:
                    # incomplete input` with no context).
                    import logging
                    _log = logging.getLogger(__name__)
                    _log.error(
                        "migrate_missing_columns_failed table=%s col=%s "
                        "sql=%r exc=%s",
                        table.name, col.name, sql, exc,
                    )
                    raise RuntimeError(
                        f"Schema migration failed on ALTER TABLE "
                        f"{table.name} ADD COLUMN {col.name}: "
                        f"{type(exc).__name__}: {exc}\n  SQL: {sql}"
                    ) from exc


def _migrate_installed_packages_unique_constraint(conn) -> None:
    """Drop the legacy unique constraint on `installed_packages.package_id`.

    The original schema shipped before per-user scoping had
    `package_id` as a standalone UNIQUE column (or the primary key).
    When we added scoping we replaced that with a composite unique
    index on `(package_id, scope, owner_user_id)` in the model, but
    SQLAlchemy's `create_all()` cannot drop constraints from an
    existing table - so daemons upgraded from an old build still carry
    the legacy column-level UNIQUE and fail to install a second scope
    of the same package with::

        UNIQUE constraint failed: installed_packages.package_id

    Detect the old constraint by inspecting `sqlite_master` for the
    CREATE TABLE statement and, if the legacy UNIQUE (or PRIMARY KEY
    on `package_id` alone) is present, rebuild the table with the
    correct schema preserving all rows.

    This is SQLite-specific; PostgreSQL deployments pre-dating the
    scoping refactor must run an Alembic migration manually.
    """
    from sqlalchemy import text

    if not conn.dialect.name == "sqlite":
        return

    has_table = conn.execute(text(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='installed_packages'"
    )).fetchone()
    if has_table is None:
        return

    # The legacy schema had `package_id` as the PK; the current
    # schema uses the surrogate `id`. Check via `PRAGMA table_info`.
    pk_columns: list[str] = []
    for row in conn.execute(text("PRAGMA table_info(installed_packages)")).fetchall():
        # Row shape: (cid, name, type, notnull, dflt_value, pk)
        if row[5] == 1:
            pk_columns.append(row[1])

    # Need rebuild iff the PK is on package_id (legacy) instead of id (new).
    needs_rebuild = pk_columns == ["package_id"]
    if not needs_rebuild:
        return

    import logging
    _log = logging.getLogger(__name__)
    _log.warning(
        "installed_packages: legacy unique constraint on package_id detected - "
        "rebuilding table with composite (package_id, scope, owner_user_id)"
    )

    # SQLite-safe rebuild: drop named indexes, rename, recreate,
    # copy rows, drop the legacy table.
    try:
        legacy_indexes = conn.execute(text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='installed_packages' "
            "  AND name NOT LIKE 'sqlite_autoindex_%'"
        )).fetchall()
        for (idx_name,) in legacy_indexes:
            conn.execute(text(f'DROP INDEX IF EXISTS "{idx_name}"'))

        conn.execute(text(
            "ALTER TABLE installed_packages RENAME TO installed_packages_legacy"
        ))

        # Re-create the new schema (CREATE TABLE + all indexes).
        Base.metadata.tables["installed_packages"].create(conn, checkfirst=True)

        # Copy rows - use INSERT OR IGNORE to silently drop duplicates
        # if the legacy table had any (shouldn't happen but be safe).
        # We explicitly list columns to tolerate schema drift.
        legacy_cols = [
            row[1] for row in conn.execute(text(
                "PRAGMA table_info(installed_packages_legacy)"
            )).fetchall()
        ]
        new_cols = [c.name for c in Base.metadata.tables["installed_packages"].columns]
        shared_cols = [c for c in new_cols if c in legacy_cols]

        # Normalise legacy defaults: empty `scope` → `'system'`;
        # empty `id` → fresh hex uuid.
        select_exprs: list[str] = []
        for col in shared_cols:
            if col == "scope":
                select_exprs.append(
                    "CASE WHEN scope IS NULL OR scope = '' "
                    "THEN 'system' ELSE scope END AS scope"
                )
            elif col == "id":
                select_exprs.append(
                    "CASE WHEN id IS NULL OR id = '' "
                    "THEN lower(hex(randomblob(16))) ELSE id END AS id"
                )
            else:
                select_exprs.append(col)

        col_list = ", ".join(shared_cols)
        select_list = ", ".join(select_exprs)
        conn.execute(text(
            f"INSERT OR IGNORE INTO installed_packages ({col_list}) "
            f"SELECT {select_list} FROM installed_packages_legacy"
        ))
        conn.execute(text("DROP TABLE installed_packages_legacy"))
        _log.info(
            "installed_packages: legacy table rebuilt successfully "
            "(scope='' → 'system' coerced for built-in rows)"
        )
    except Exception as exc:
        _log.error(
            "installed_packages legacy rebuild failed: %s - "
            "rolling back rename", exc, exc_info=True,
        )
        # Best-effort rollback
        try:
            conn.execute(text("DROP TABLE IF EXISTS installed_packages"))
            conn.execute(text(
                "ALTER TABLE installed_packages_legacy RENAME TO installed_packages"
            ))
        except Exception as exc:
            logger.debug("database best-effort block failed: %s", exc)
        raise


def _migrate_applications_drop_app_id_unique(conn) -> None:
    """Replace the legacy `app_id` UNIQUE with a composite scope-aware index.

    Pre-multi-tenant, `applications.app_id` was declared UNIQUE, which
    prevents the same app_id from being installed by two users (or a user
    install coexisting with a system install). The new schema drops that
    column-level UNIQUE and adds a composite unique index on
    `(app_id, scope, owner_user_id)` instead.

    SQLite cannot drop a column-level UNIQUE in-place - the only way is to
    rebuild the table. This helper is idempotent: it detects the legacy
    constraint, rebuilds the table while preserving every row and every
    FK-pointing bundle/profile/session, and then populates the new
    `scope` / `owner_user_id` columns with `'system'` / `''` for
    existing rows (backwards-compatible - legacy deploys were all system).

    Runs only on SQLite. Other engines get a warning and are expected to
    migrate via Alembic.
    """
    from sqlalchemy import text

    if conn.dialect.name != "sqlite":
        return

    has_table = conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='applications'"
    )).fetchone()
    if has_table is None:
        return

    # Read the CREATE TABLE statement - it tells us whether app_id still
    # carries the legacy column-level UNIQUE keyword.
    create_sql_row = conn.execute(text(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='applications'"
    )).fetchone()
    if not create_sql_row or not create_sql_row[0]:
        return
    create_sql = create_sql_row[0]

    # Legacy schema had either `app_id VARCHAR(255) UNIQUE` inline in the
    # CREATE TABLE, or an ancillary `CREATE UNIQUE INDEX ix_applications_app_id
    # ON applications (app_id)` - both prevent the multi-tenant schema.
    has_legacy_unique_column = any(
        "app_id" in line and "UNIQUE" in line.upper()
        for line in create_sql.splitlines()
    )
    has_legacy_unique_index = conn.execute(text(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='index' "
        "  AND tbl_name='applications' "
        "  AND sql LIKE '%UNIQUE INDEX%app_id%' "
        "  AND sql NOT LIKE '%scope%'"
    )).fetchone() is not None
    if not (has_legacy_unique_column or has_legacy_unique_index):
        return

    # Fast path: if only the legacy unique index is present (column-level
    # UNIQUE was already absent from CREATE TABLE), just drop the index.
    if has_legacy_unique_index and not has_legacy_unique_column:
        conn.execute(text("DROP INDEX IF EXISTS ix_applications_app_id"))
        # Recreate the composite unique index defined in models.
        try:
            Base.metadata.tables["applications"].indexes  # trigger lazy build
            for idx in Base.metadata.tables["applications"].indexes:
                if idx.name == "ix_applications_scope_key":
                    idx.create(conn, checkfirst=True)
                    break
        except Exception as exc:
            logger.debug("database best-effort block failed: %s", exc)
        import logging
        _log = logging.getLogger(__name__)
        _log.info(
            "applications: dropped legacy unique index ix_applications_app_id; "
            "composite (app_id, scope, owner_user_id) now active."
        )
        return

    import logging
    _log = logging.getLogger(__name__)
    _log.warning(
        "applications: legacy UNIQUE on app_id detected - rebuilding table "
        "with composite (app_id, scope, owner_user_id) index."
    )

    try:
        # Drop named indexes that would collide after rebuild.
        legacy_indexes = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "  AND tbl_name='applications' AND name NOT LIKE 'sqlite_autoindex_%'"
        )).fetchall()
        for (idx_name,) in legacy_indexes:
            conn.execute(text(f'DROP INDEX IF EXISTS "{idx_name}"'))

        conn.execute(text("ALTER TABLE applications RENAME TO applications_legacy"))

        # Re-create the correct schema (CREATE TABLE + new composite index).
        Base.metadata.tables["applications"].create(conn, checkfirst=True)

        # Copy rows - back-fill scope='system' and owner_user_id='' for any
        # legacy row that doesn't already have them.
        legacy_cols = [
            row[1] for row in conn.execute(text(
                "PRAGMA table_info(applications_legacy)"
            )).fetchall()
        ]
        new_cols = [c.name for c in Base.metadata.tables["applications"].columns]
        shared_cols = [c for c in new_cols if c in legacy_cols]

        select_exprs: list[str] = []
        for col in shared_cols:
            if col == "scope":
                select_exprs.append(
                    "CASE WHEN scope IS NULL OR scope = '' "
                    "THEN 'system' ELSE scope END AS scope"
                )
            elif col == "owner_user_id":
                select_exprs.append(
                    "CASE WHEN owner_user_id IS NULL THEN '' "
                    "ELSE owner_user_id END AS owner_user_id"
                )
            else:
                select_exprs.append(col)

        # If scope / owner_user_id were not in the legacy table at all, we
        # still need them in the INSERT - supply constant defaults.
        if "scope" not in shared_cols:
            shared_cols.append("scope")
            select_exprs.append("'system' AS scope")
        if "owner_user_id" not in shared_cols:
            shared_cols.append("owner_user_id")
            select_exprs.append("'' AS owner_user_id")

        col_list = ", ".join(shared_cols)
        select_list = ", ".join(select_exprs)
        conn.execute(text(
            f"INSERT OR IGNORE INTO applications ({col_list}) "
            f"SELECT {select_list} FROM applications_legacy"
        ))
        conn.execute(text("DROP TABLE applications_legacy"))
        _log.info(
            "applications: legacy table rebuilt - scope/owner_user_id back-filled."
        )
    except Exception as exc:
        _log.error(
            "applications legacy rebuild failed: %s - rolling back rename.",
            exc, exc_info=True,
        )
        try:
            conn.execute(text("DROP TABLE IF EXISTS applications"))
            conn.execute(text(
                "ALTER TABLE applications_legacy RENAME TO applications"
            ))
        except Exception as exc:
            logger.debug("database best-effort block failed: %s", exc)
        raise


def _migrate_history_log_seq_unique(conn) -> None:
    """Add the partial UNIQUE indexes that enforce per-scope monotonic
    seq on `history_log` for `kind='event'` rows.

    The model declares the indexes via `__table_args__` so a fresh
    `create_all` already produces them. Existing DBs need the
    explicit `CREATE UNIQUE INDEX IF NOT EXISTS` because SQLAlchemy
    skips index creation on tables that already exist.

    Ordering invariant the indexes enforce:

        - per session  : `UNIQUE (session_id, seq) WHERE kind='event'
                          AND session_id IS NOT NULL`
        - per user-only: `UNIQUE (user_id,    seq) WHERE kind='event'
                          AND session_id IS NULL`

    If existing rows already violate the invariant (legacy rows from
    the seq-seed bug where module events restarted the counter at 1
    after a daemon restart), the CREATE will fail with an integrity
    error. We log the failure with enough context for the operator
    to clean up - the daemon keeps running without the constraint
    rather than refusing to start. New rows still go through the
    fixed in-memory counter, so duplicates stop happening.

    Both SQLite (3.8+) and Postgres support the `WHERE` clause on
    UNIQUE INDEX. Older SQLite would silently ignore the WHERE; the
    daemon already documents Python 3.12 + a recent SQLite, so this
    is fine.
    """
    from sqlalchemy import text

    import logging
    _log = logging.getLogger(__name__)

    # Dialect-aware table existence check - skip the migration entirely
    # when the table hasn't been created yet (fresh DB, create_all
    # below will produce the indexes from the model declaration).
    if conn.dialect.name == "sqlite":
        has_table = conn.execute(text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='history_log'"
        )).fetchone()
    else:
        has_table = conn.execute(text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'history_log'"
        )).fetchone()
    if has_table is None:
        return

    # `(session_id, seq, kind)` is the chat-ordering invariant; the
    # `kind` axis lets `message` and `event` share a seq.
    statements = [
        (
            "ix_history_session_seq_unique",
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_history_session_seq_unique "
            "ON history_log (session_id, seq, kind) "
            "WHERE session_id IS NOT NULL "
            "AND seq IS NOT NULL AND seq > 0",
        ),
        (
            "ix_history_user_seq_event_unique",
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "ix_history_user_seq_event_unique "
            "ON history_log (user_id, seq) "
            "WHERE kind = 'event' AND session_id IS NULL",
        ),
    ]

    for index_name, sql in statements:
        try:
            conn.execute(text(sql))
        except Exception as exc:
            _log.warning(
                "history_log_seq_unique_index_failed name=%s: %s "
                "(legacy duplicates may exist; new rows are still "
                "monotonic via the fixed in-memory counter)",
                index_name, exc,
            )


async def close_db() -> None:
    """Dispose the engine. Called at daemon shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session. Use as an async context manager or FastAPI Depends."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    async with _session_factory() as session:
        yield session


def get_session_factory() -> "async_sessionmaker[AsyncSession]":
    """Return the session factory honouring the per-coro override.

    The off-loop persist worker (`runtime/persist_worker.py`) sets
    `_session_factory_override` to its own loop-bound factory before
    running each job. Without this routing, persist coros would try
    to grab connections from the daemon's main pool - whose asyncpg
    connections are bound to the main event loop - and crash with
    `Future attached to a different loop`.

    ContextVars propagate through `await` chains and through
    `asyncio.create_task` (Python 3.7+ semantics), so the override
    automatically reaches every nested coroutine without any
    parameter threading.
    """
    override = _session_factory_override.get()
    if override is not None:
        return override
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


def get_engine() -> AsyncEngine:
    """Return the current engine (for raw operations or migrations)."""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine
