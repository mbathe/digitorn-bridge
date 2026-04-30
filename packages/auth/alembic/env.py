"""Alembic env for digitorn-auth.

Reads the database URL from ``DIGITORN_AUTH_DATABASE_URL`` (matches
the runtime config). Targets the metadata of ``digitorn_auth.database.Base``
so autogenerate produces deltas only for the auth-owned tables.

When deployed on the same Postgres as the daemon, run migrations
with ``-x compare_type=true`` so we don't trip on legacy types
(e.g. ``LargeBinary`` already present from the daemon's schema).
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from digitorn_auth.database import Base
import digitorn_auth.models  # noqa: F401  -- register tables

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    return (
        os.environ.get("DIGITORN_AUTH_DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )


def run_migrations_offline() -> None:
    """Generate migration SQL without connecting to a DB.

    Useful for review / Postgres-aware syntax checks before applying.
    """
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # Skip tables we don't own (the daemon's `applications`,
        # `history_log`, etc. live in the same Postgres). Without
        # this filter, autogenerate would propose to DROP them.
        include_object=_include_only_owned,
    )
    with context.begin_transaction():
        context.run_migrations()


_OWNED_TABLES = {
    "users",
    "user_oauth_tokens",
    "roles",
    "user_roles",
    "refresh_tokens",
    "paired_devices",
}


def _include_only_owned(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in _OWNED_TABLES
    return True


async def run_async_migrations() -> None:
    config_section = config.get_section(config.config_ini_section, {})
    config_section["sqlalchemy.url"] = _resolve_url()
    connectable = async_engine_from_config(
        config_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
