"""Named connection pool for the database module.

Each connection is identified by a ``connection_id`` string. The pool
manages adapter lifecycle (connect/disconnect) and ensures cleanup on
module stop.

Thread-safety: All operations are async and run on the event loop.
No locking is needed - asyncio is single-threaded.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from .adapters.base import DatabaseAdapter
from .adapters.sql import SQLAdapter

logger = structlog.get_logger(__name__)


def _get_mongo_adapter_class() -> type:
    """Lazy import to avoid hard dependency on motor."""
    from .adapters.mongo import MongoAdapter
    return MongoAdapter


def _get_redis_adapter_class() -> type:
    """Lazy import to avoid hard dependency on redis."""
    from .adapters.redis import RedisAdapter
    return RedisAdapter


_ADAPTER_FACTORIES: dict[str, type | callable] = {
    "sqlite": SQLAdapter,
    "postgresql": SQLAdapter,
    "mysql": SQLAdapter,
    "mssql": SQLAdapter,
    "oracle": SQLAdapter,
    "mongodb": _get_mongo_adapter_class,
    "redis": _get_redis_adapter_class,
}

_URL_SCHEMES: dict[str, str] = {
    "sqlite": "sqlite+aiosqlite",
    "postgresql": "postgresql+asyncpg",
    "mysql": "mysql+aiomysql",
    "mssql": "mssql+aioodbc",
    "oracle": "oracle+oracledb",
    "mongodb": "mongodb",
    "redis": "redis",
}


@dataclass
class ConnectionEntry:
    """Metadata for a named connection."""

    connection_id: str
    adapter: DatabaseAdapter
    driver: str
    url_display: str
    created_at: float = field(default_factory=time.time)
    guard: Any = None


class ConnectionPool:
    """Named connection pool - maps connection_id → adapter."""

    def __init__(self) -> None:
        self._connections: dict[str, ConnectionEntry] = {}

    def __contains__(self, connection_id: str) -> bool:
        return connection_id in self._connections

    def __len__(self) -> int:
        return len(self._connections)

    def get(self, connection_id: str) -> ConnectionEntry:
        """Get a connection by ID. Raises KeyError if not found."""
        entry = self._connections.get(connection_id)
        if not entry:
            raise KeyError(
                f"Connection '{connection_id}' not found. "
                f"Available: {list(self._connections.keys())}"
            )
        return entry

    def get_adapter(self, connection_id: str) -> DatabaseAdapter:
        """Shortcut: get the adapter for a connection."""
        return self.get(connection_id).adapter

    async def connect(
        self,
        connection_id: str,
        driver: str,
        *,
        database: str | None = None,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        url: str | None = None,
        options: dict[str, Any] | None = None,
        allowed_hosts: list[str] | None = None,
    ) -> ConnectionEntry:
        """Open a new named connection.

        Either provide ``url`` directly, or ``driver`` + connection params
        to build the URL automatically.
        """
        if connection_id in self._connections:
            await self.disconnect(connection_id)

        if url is None:
            url = self._build_url(driver, database, host, port, username, password, allowed_hosts=allowed_hosts)

        factory = _ADAPTER_FACTORIES.get(driver)
        if not factory:
            raise ValueError(
                f"Unsupported driver: {driver!r}. "
                f"Available: {list(_ADAPTER_FACTORIES.keys())}"
            )

        if callable(factory) and not isinstance(factory, type):
            factory = factory()

        adapter = factory()
        await adapter.connect(url, **(options or {}))

        entry = ConnectionEntry(
            connection_id=connection_id,
            adapter=adapter,
            driver=driver,
            url_display=_sanitize_url(url),
        )
        self._connections[connection_id] = entry

        await logger.ainfo(
            "connection_opened",
            connection_id=connection_id,
            driver=driver,
            url=entry.url_display,
        )
        return entry

    async def disconnect(self, connection_id: str) -> None:
        """Close and remove a named connection."""
        entry = self._connections.pop(connection_id, None)
        if entry:
            await entry.adapter.disconnect()
            await logger.ainfo(
                "connection_closed",
                connection_id=connection_id,
            )

    async def disconnect_all(self) -> int:
        """Close all connections. Returns count closed."""
        ids = list(self._connections.keys())
        for cid in ids:
            await self.disconnect(cid)
        return len(ids)

    def list_connections(self) -> list[dict[str, Any]]:
        """Return metadata for all active connections."""
        return [
            {
                "connection_id": e.connection_id,
                "driver": e.driver,
                "url": e.url_display,
                "connected": e.adapter.connected,
                "age_seconds": round(time.time() - e.created_at, 1),
            }
            for e in self._connections.values()
        ]


    @staticmethod
    def _build_url(
        driver: str,
        database: str | None,
        host: str | None,
        port: int | None,
        username: str | None,
        password: str | None,
        allowed_hosts: list[str] | None = None,
    ) -> str:
        """Build a connection URL from components."""
        scheme = _URL_SCHEMES.get(driver)
        if not scheme:
            raise ValueError(f"No URL scheme for driver: {driver!r}")

        _SAFE_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
        if driver != "sqlite" and host:
            if host.lower() not in _SAFE_HOSTS:
                _extra = allowed_hosts or []
                if not any(host.lower() == h.lower() for h in _extra):
                    raise ValueError(
                        f"Database connections to remote host '{host}' are blocked by default. "
                        f"Only localhost and declared hosts are allowed. "
                        f"Add: modules.database.constraints.allowed_hosts: ['{host}']"
                    )

        if driver == "sqlite":
            path = database or ":memory:"
            if path != ":memory:":
                resolved = os.path.realpath(path)
                _BLOCKED_PREFIXES = ("/etc", "/proc", "/sys", "/dev")
                for prefix in _BLOCKED_PREFIXES:
                    if resolved.startswith(prefix):
                        raise ValueError(
                            f"SQLite path blocked (system directory): {resolved}"
                        )
                parts = resolved.split(os.sep)
                for part in parts:
                    if part.startswith(".") and part not in (".", ".."):
                        raise ValueError(
                            f"SQLite path blocked (hidden directory): {resolved}"
                        )
                _ALLOWED_EXTENSIONS = (
                    ".db", ".sqlite", ".sqlite3", ".s3db", ".db3",
                )
                if not any(resolved.endswith(ext) for ext in _ALLOWED_EXTENSIONS):
                    raise ValueError(
                        f"SQLite path blocked (suspicious extension): {resolved} "
                        f"- expected one of {_ALLOWED_EXTENSIONS}"
                    )
                path = resolved
            return f"{scheme}:///{path}"

        if driver == "mongodb":
            return _build_mongo_url(
                database, host, port, username, password,
            )

        if driver == "redis":
            return _build_redis_url(host, port, password, database)

        if not database:
            raise ValueError(f"database is required for driver={driver!r}")

        userinfo = ""
        if username:
            userinfo = username
            if password:
                userinfo += f":{password}"
            userinfo += "@"

        hostport = host or "localhost"
        if port:
            hostport += f":{port}"

        return f"{scheme}://{userinfo}{hostport}/{database}"


def _sanitize_url(url: str) -> str:
    """Remove password from URL for display."""
    if "@" in url and ":" in url.split("@")[0]:
        parts = url.split("@")
        creds = parts[0]
        scheme_user = creds.rsplit(":", 1)[0]
        return f"{scheme_user}:****@{parts[1]}"
    return url


def _build_mongo_url(
    database: str | None,
    host: str | None,
    port: int | None,
    username: str | None,
    password: str | None,
) -> str:
    """Build a MongoDB connection URL."""
    userinfo = ""
    if username:
        userinfo = username
        if password:
            userinfo += f":{password}"
        userinfo += "@"

    hostport = host or "localhost"
    if port:
        hostport += f":{port}"
    else:
        hostport += ":27017"

    db = f"/{database}" if database else "/test"
    return f"mongodb://{userinfo}{hostport}{db}"


def _build_redis_url(
    host: str | None,
    port: int | None,
    password: str | None,
    database: str | None,
) -> str:
    """Build a Redis connection URL."""
    auth = ""
    if password:
        auth = f":{password}@"

    hostport = host or "localhost"
    port_num = port or 6379
    db_index = f"/{database}" if database else "/0"
    return f"redis://{auth}{hostport}:{port_num}{db_index}"
