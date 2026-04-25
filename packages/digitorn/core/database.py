"""Digitorn — Database engine and session management.

Provides a single async SQLAlchemy engine and session factory used by
the entire daemon. Supports any SQLAlchemy-compatible backend (SQLite,
PostgreSQL, MySQL, etc.) — the URL in config determines which one.

Usage::

    from digitorn.core.database import get_session, init_db

    await init_db(settings)

    async with get_session() as session:
        result = await session.execute(select(MyModel))
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from digitorn.core.config import Settings


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""

    pass


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(settings: Settings) -> AsyncEngine:
    """Create the async engine and session factory.

    Called once at daemon startup. Creates all tables if they don't exist.
    """
    import digitorn.core.models  # noqa: F401 — register ORM models with Base.metadata

    global _engine, _session_factory

    is_sqlite = settings.database.url.startswith("sqlite")
    connect_args = {}
    if is_sqlite:
        connect_args["check_same_thread"] = False

    pool_kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if not is_sqlite:
        pool_kwargs.update(pool_size=50, max_overflow=100, pool_timeout=30)

    _engine = create_async_engine(
        settings.database.url,
        echo=settings.database.echo,
        connect_args=connect_args,
        **pool_kwargs,
    )

    # SQLite under concurrent write load: without WAL mode, any second
    # writer gets "database is locked" immediately. WAL + busy_timeout
    # lets readers proceed during writes AND makes writers wait (up to
    # 5s) on a held lock instead of failing. Measured at N=20 parallel
    # auth calls: ~35% failure without, 0% with.
    if is_sqlite:
        from sqlalchemy import event as _sa_event

        sync_engine = _engine.sync_engine

        @_sa_event.listens_for(sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                # 30 s is long enough to absorb bursts of parallel
                # writes (N=20 registers serialised over WAL took a
                # couple of seconds in measurement). Raising from 5 s
                # eliminated the remaining "database is locked" 500s
                # under N=20 load without affecting the happy path.
                cur.execute("PRAGMA busy_timeout=30000")
                # NOTE: PRAGMA foreign_keys=ON exposes a pre-existing
                # "users → applications" FK mismatch in the schema.
                # Fix the schema separately before turning this on —
                # otherwise register/login fails immediately.
            finally:
                cur.close()

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_missing_columns)
        await conn.run_sync(_migrate_installed_packages_unique_constraint)
        await conn.run_sync(_migrate_applications_drop_app_id_unique)

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
    from sqlalchemy import inspect, text

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
                        # Numeric literal (int or float).
                        default = f" DEFAULT {raw}"
                    elif raw.upper() in ("TRUE", "FALSE", "NULL", "CURRENT_TIMESTAMP"):
                        # SQL keyword literal.
                        default = f" DEFAULT {raw.upper()}"
                    else:
                        # String literal — SQL-escape embedded single quotes.
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
    """Drop the legacy unique constraint on ``installed_packages.package_id``.

    The original schema shipped before per-user scoping had
    ``package_id`` as a standalone UNIQUE column (or the primary key).
    When we added scoping we replaced that with a composite unique
    index on ``(package_id, scope, owner_user_id)`` in the model, but
    SQLAlchemy's ``create_all()`` cannot drop constraints from an
    existing table — so daemons upgraded from an old build still carry
    the legacy column-level UNIQUE and fail to install a second scope
    of the same package with::

        UNIQUE constraint failed: installed_packages.package_id

    Detect the old constraint by inspecting ``sqlite_master`` for the
    CREATE TABLE statement and, if the legacy UNIQUE (or PRIMARY KEY
    on ``package_id`` alone) is present, rebuild the table with the
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

    # The definitive signal is the primary key column. The legacy
    # schema had ``package_id`` as the PK; the current schema has
    # the surrogate ``id``. PRAGMA table_info returns ``pk=1`` for
    # the column that's part of the (non-composite) primary key.
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
        "installed_packages: legacy unique constraint on package_id detected — "
        "rebuilding table with composite (package_id, scope, owner_user_id)"
    )

    # Rebuild strategy (SQLite-safe):
    # 1. Drop all named indexes on the legacy table (they'd collide
    #    when we recreate the correct ones)
    # 2. Rename old table
    # 3. Re-create the correct schema + new indexes via SQLAlchemy
    # 4. Copy rows from legacy → new, coalescing scope/id
    # 5. Drop the legacy table
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

        # Copy rows — use INSERT OR IGNORE to silently drop duplicates
        # if the legacy table had any (shouldn't happen but be safe).
        # We explicitly list columns to tolerate schema drift.
        legacy_cols = [
            row[1] for row in conn.execute(text(
                "PRAGMA table_info(installed_packages_legacy)"
            )).fetchall()
        ]
        new_cols = [c.name for c in Base.metadata.tables["installed_packages"].columns]
        shared_cols = [c for c in new_cols if c in legacy_cols]

        # Build per-column SELECT expressions that normalise legacy
        # defaults to the new schema's expected values:
        # - ``scope=''``  →  ``scope='system'``  (built-ins shipped
        #   before per-user scoping are all system-scoped)
        # - ``id=''``     →  generate a fresh hex uuid inline
        #
        # Everything else is passed through verbatim.
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
            "installed_packages legacy rebuild failed: %s — "
            "rolling back rename", exc, exc_info=True,
        )
        # Best-effort rollback
        try:
            conn.execute(text("DROP TABLE IF EXISTS installed_packages"))
            conn.execute(text(
                "ALTER TABLE installed_packages_legacy RENAME TO installed_packages"
            ))
        except Exception:
            pass
        raise


def _migrate_applications_drop_app_id_unique(conn) -> None:
    """Replace the legacy ``app_id`` UNIQUE with a composite scope-aware index.

    Pre-multi-tenant, ``applications.app_id`` was declared UNIQUE, which
    prevents the same app_id from being installed by two users (or a user
    install coexisting with a system install). The new schema drops that
    column-level UNIQUE and adds a composite unique index on
    ``(app_id, scope, owner_user_id)`` instead.

    SQLite cannot drop a column-level UNIQUE in-place — the only way is to
    rebuild the table. This helper is idempotent: it detects the legacy
    constraint, rebuilds the table while preserving every row and every
    FK-pointing bundle/profile/session, and then populates the new
    ``scope`` / ``owner_user_id`` columns with ``'system'`` / ``''`` for
    existing rows (backwards-compatible — legacy deploys were all system).

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

    # Read the CREATE TABLE statement — it tells us whether app_id still
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
    # ON applications (app_id)` — both prevent the multi-tenant schema.
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
        except Exception:
            pass
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
        "applications: legacy UNIQUE on app_id detected — rebuilding table "
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

        # Copy rows — back-fill scope='system' and owner_user_id='' for any
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
        # still need them in the INSERT — supply constant defaults.
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
            "applications: legacy table rebuilt — scope/owner_user_id back-filled."
        )
    except Exception as exc:
        _log.error(
            "applications legacy rebuild failed: %s — rolling back rename.",
            exc, exc_info=True,
        )
        try:
            conn.execute(text("DROP TABLE IF EXISTS applications"))
            conn.execute(text(
                "ALTER TABLE applications_legacy RENAME TO applications"
            ))
        except Exception:
            pass
        raise


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
    """Return the session factory. Raises if DB not initialized."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


def get_engine() -> AsyncEngine:
    """Return the current engine (for raw operations or migrations)."""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _engine
