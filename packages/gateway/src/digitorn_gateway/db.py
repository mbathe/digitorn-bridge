"""Async SQLAlchemy session factory.

The gateway shares the auth service's Postgres database. Tables added
by the gateway live in the same schema as `users` so a single JOIN
can resolve `(user_id) -> plan -> limits` without crossing DB boundaries.

In dev the default URL is `sqlite+aiosqlite:///./gateway.db` which gives
operators a single-file local DB they can inspect and reset at will.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> None:
    """Build the async engine + session factory using the live config.

    Idempotent - safe to call multiple times. Called from the FastAPI
    lifespan hook before any route handler runs.
    """
    global _engine, _session_factory

    from digitorn_gateway.config import get_settings
    settings = get_settings()

    if _engine is not None:
        return

    _engine = create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    logger.info("db_engine_initialised url=%s", _redact_url(settings.database_url))


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError(
            "db.init_engine() must be called before get_session_factory()",
        )
    return _session_factory


async def session_dependency() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Yields one session per request, closes on exit."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def _redact_url(url: str) -> str:
    """Hide the password in DB URLs for logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(url)
        if p.password:
            netloc = p.netloc.replace(f":{p.password}@", ":****@")
            return urlunparse(p._replace(netloc=netloc))
    except Exception:
        pass
    return url
