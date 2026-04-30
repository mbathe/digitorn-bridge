"""Async SQLAlchemy engine + session factory + declarative base.

Standalone-friendly: this package owns its own engine so the auth
service can run without the daemon. The schema is deliberately a
SUBSET of the daemon's `digitorn.core.database` (only the tables
auth needs: users, roles, user_roles, refresh_tokens, user_oauth_tokens,
paired_devices). When deployed against the same Postgres as the daemon,
both packages see the same physical tables.
"""

from __future__ import annotations

import logging
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base shared by all auth models."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    """Create the async engine. Idempotent — repeat calls return the
    same engine. Pass a fresh URL only on first init.
    """
    global _engine, _session_factory
    if _engine is not None:
        return _engine

    logger.info("auth_db_init url=%s", _redact(database_url))
    _engine = create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(
        _engine, expire_on_commit=False, class_=AsyncSession,
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError(
            "auth engine not initialised - call init_engine() first "
            "(usually done in server.py lifespan)."
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError(
            "auth session factory not initialised - call init_engine() first."
        )
    return _session_factory


async def dispose_engine() -> None:
    """Close all connections at shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def _redact(url: str) -> str:
    """Strip password from connection URL for safe logging."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0] if ":" in creds else creds
    return f"{scheme}://{user}:***@{host}"
