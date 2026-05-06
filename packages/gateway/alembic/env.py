"""Alembic env. Reads the live DATABASE_URL from settings (NEVER from
alembic.ini's placeholder), so the migrations target whichever Postgres
the gateway is currently configured for.

The gateway's tables coexist with auth's ``users`` table in the same
Postgres. Migrations here only touch the ``gateway_*`` tables - the
``users`` table is owned by ``digitorn-auth`` and migrated separately.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from digitorn_gateway.config import get_settings  # noqa: E402
from digitorn_gateway.models_db import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The gateway's models live in `Base.metadata`. Limit autogenerate to
# the `gateway_*` tables so a future autogenerate run doesn't try to
# manage the `users` table that belongs to auth.
target_metadata = Base.metadata


def _resolve_url() -> str:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError(
            "DIGITORN_GATEWAY_DATABASE_URL must be set to run migrations.",
        )
    # Alembic uses sync drivers; rewrite the asyncpg URL into the
    # psycopg (v3) sync driver and translate the libpq ssl arg name
    # (asyncpg uses ``ssl=require``, libpq/psycopg uses
    # ``sslmode=require``).
    url = settings.database_url
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    url = url.replace("?ssl=require", "?sslmode=require")
    url = url.replace("&ssl=require", "&sslmode=require")
    return url


def _include_object(obj, name, type_, reflected, compare_to):
    """Filter so only `gateway_*` tables are touched. Otherwise alembic
    autogenerate would propose to drop the auth tables it sees in the
    DB but doesn't know about."""
    if type_ == "table":
        return name.startswith("gateway_")
    if type_ == "index" or type_ == "foreign_key_constraint" or type_ == "unique_constraint":
        # Tied to a table - keep when the parent table is ours.
        if hasattr(obj, "table") and obj.table is not None:
            tname = obj.table.name
            return tname.startswith("gateway_")
    return True


VERSION_TABLE = "alembic_version_gateway"


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        compare_type=True,
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            compare_type=True,
            version_table=VERSION_TABLE,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
