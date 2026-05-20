"""Daemon-level MCP Connection Pool - shared across all apps.

Unlike the per-app MCPConnectionPool in mcp/connections.py, this pool
lives at the daemon level and is shared by all deployed applications.

Key features:
- **Ref-counting**: tracks which apps use which servers. When ref drops
  to 0, the connection is released (configurable: keep-alive or disconnect).
- **Health monitoring**: periodic background task pings all connected servers.
- **Auto-start**: on daemon boot, connects to all auto_start=True servers from DB.
- **Single connection per server**: apps share the same transport connection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from collections.abc import Callable, Coroutine
from enum import Enum

from digitorn.modules.mcp.connections import MCPConnectionPool, MCPServerEntry
from digitorn.modules.mcp.transports import MCPTransportError

logger = logging.getLogger(__name__)


class MCPServerEvent(str, Enum):
    """Events emitted when a daemon-managed MCP server changes state."""
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONFIG_UPDATED = "config_updated"


ServerEventCallback = Callable[[MCPServerEvent, str], Coroutine[Any, Any, None]]

_HEALTH_INTERVAL = 60


@dataclass
class _ServerRef:
    """Ref-counting wrapper for a daemon-level MCP connection."""
    server_id: str
    app_ids: set[str] = field(default_factory=set)


class DaemonMCPPool:
    """Daemon-level shared MCP connection pool with ref-counting.

    One instance per daemon, stored in `app.state.mcp_pool`.
    Apps acquire/release servers, and the pool manages connections.
    """

    def __init__(self) -> None:
        self._pool = MCPConnectionPool()
        self._refs: dict[str, _ServerRef] = {}
        self._health_task: asyncio.Task[None] | None = None
        self._running = False
        self._session_factory: Any | None = None
        self._on_event: ServerEventCallback | None = None

    def set_on_event(self, callback: ServerEventCallback) -> None:
        """Register a callback invoked when a server changes state.

        The callback receives `(event, server_id)` and should update
        tool indexes for any apps that depend on this server.
        """
        self._on_event = callback

    async def _emit(self, event: MCPServerEvent, server_id: str) -> None:
        """Fire the event callback (best-effort, never raises)."""
        if self._on_event is None:
            return
        try:
            await self._on_event(event, server_id)
        except Exception as exc:
            logger.warning(
                "daemon_mcp_event_callback_error event=%s server=%s: %s",
                event.value, server_id, exc,
            )

    @property
    def pool(self) -> MCPConnectionPool:
        """Access the underlying connection pool."""
        return self._pool


    async def start(self, session_factory: Any) -> None:
        """Start the daemon pool: auto-connect servers from DB, start health monitor."""
        from digitorn.core.mcp_store import list_servers as db_list_servers, _build_connect_kwargs

        self._running = True
        self._session_factory = session_factory

        try:
            async with session_factory() as session:
                servers = await db_list_servers(session)
                for server in servers:
                    if not server.auto_start:
                        continue
                    if server.status not in ("installed", "tested", "ready"):
                        continue
                    try:
                        kwargs = _build_connect_kwargs(server)
                        await self._pool.connect(server.server_id, server.transport, **kwargs)
                        self._refs[server.server_id] = _ServerRef(server_id=server.server_id)
                        logger.info("daemon_mcp_auto_started server=%s", server.server_id)
                    except MCPTransportError as exc:
                        logger.warning(
                            "daemon_mcp_auto_start_failed server=%s: %s",
                            server.server_id, exc,
                        )
        except Exception as exc:
            logger.warning("daemon_mcp_start_db_error: %s", exc)

        self._health_task = asyncio.create_task(self._health_loop())

    async def stop(self) -> None:
        """Stop the daemon pool: cancel health monitor, disconnect all."""
        self._running = False
        if self._health_task is not None and not self._health_task.done():
            self._health_task.cancel()
            try:
                await asyncio.wait_for(self._health_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.warning("mcp_pool_health_task_force_stopped")
            self._health_task = None

        closed = await self._pool.disconnect_all()
        self._refs.clear()
        if closed:
            logger.info("daemon_mcp_pool_stopped servers_closed=%d", closed)


    async def acquire(
        self,
        server_id: str,
        app_id: str,
        transport: str,
        **connect_kwargs: Any,
    ) -> MCPServerEntry:
        """Acquire a server connection for an app.

        If not already connected, creates the connection.
        Increments the ref count for this app.
        """
        ref = self._refs.get(server_id)
        if ref is not None:
            ref.app_ids.add(app_id)
            entry = self._pool.get_server(server_id)
            if entry is not None and entry.status == "connected":
                # Warn if the new caller passed different kwargs (env, headers, etc.)
                stored = entry._connect_kwargs
                new_kw = {"transport_type": transport, **connect_kwargs}
                if stored.get("env") != new_kw.get("env") or stored.get("headers") != new_kw.get("headers"):
                    logger.warning(
                        "daemon_mcp_kwargs_mismatch server=%s app=%s - "
                        "shared connection uses original credentials; "
                        "app-specific overrides are ignored",
                        server_id, app_id,
                    )
                return entry
            try:
                entry = await self._pool.reconnect(server_id)
                return entry
            except MCPTransportError:
                pass

        entry = await self._pool.connect(server_id, transport, **connect_kwargs)
        if server_id not in self._refs:
            self._refs[server_id] = _ServerRef(server_id=server_id)
        self._refs[server_id].app_ids.add(app_id)

        logger.info(
            "daemon_mcp_acquired server=%s app=%s refs=%d",
            server_id, app_id, len(self._refs[server_id].app_ids),
        )
        return entry

    async def release(self, server_id: str, app_id: str) -> None:
        """Release a server connection for an app.

        Decrements ref count. Keeps connection alive (other apps may use it).
        """
        ref = self._refs.get(server_id)
        if ref is None:
            return

        ref.app_ids.discard(app_id)

        logger.info(
            "daemon_mcp_released server=%s app=%s refs=%d",
            server_id, app_id, len(ref.app_ids),
        )


    def get_refs(self, server_id: str) -> set[str]:
        """Get app_ids currently using a server."""
        ref = self._refs.get(server_id)
        return ref.app_ids.copy() if ref else set()


    async def connect_server(
        self,
        server_id: str,
        transport: str,
        **kwargs: Any,
    ) -> MCPServerEntry:
        """Connect a server without app ref (daemon-level management)."""
        entry = await self._pool.connect(server_id, transport, **kwargs)
        if server_id not in self._refs:
            self._refs[server_id] = _ServerRef(server_id=server_id)
        await self._emit(MCPServerEvent.CONNECTED, server_id)
        return entry

    async def disconnect_server(self, server_id: str) -> None:
        """Disconnect a server (force - ignores ref count)."""
        await self._pool.disconnect(server_id)
        self._refs.pop(server_id, None)
        await self._emit(MCPServerEvent.DISCONNECTED, server_id)

    def get_server(self, server_id: str) -> MCPServerEntry | None:
        """Get server entry (for introspection)."""
        return self._pool.get_server(server_id)

    def list_connected(self) -> list[dict[str, Any]]:
        """List all connected servers with ref info."""
        result = []
        for info in self._pool.list_servers():
            sid = info["server_id"]
            ref = self._refs.get(sid)
            info["app_refs"] = sorted(ref.app_ids) if ref else []
            info["ref_count"] = len(ref.app_ids) if ref else 0
            result.append(info)
        return result


    async def _health_loop(self) -> None:
        """Background task: periodic health checks on all connected servers."""
        while self._running:
            try:
                await asyncio.sleep(_HEALTH_INTERVAL)
            except asyncio.CancelledError:
                return

            for server_id in list(self._refs.keys()):
                entry = self._pool.get_server(server_id)
                if entry is None:
                    continue

                reconnected = False

                if entry.status in ("error", "disconnected"):
                    try:
                        await self._pool.reconnect(server_id)
                        logger.info("daemon_mcp_health_retry_ok server=%s", server_id)
                        reconnected = True
                    except MCPTransportError as exc:
                        logger.debug("daemon_mcp_health_retry_fail server=%s: %s", server_id, exc)
                else:
                    try:
                        ok = await self._pool.ping(server_id)
                        if not ok:
                            logger.warning("daemon_mcp_health_fail server=%s - attempting reconnect", server_id)
                            try:
                                await self._pool.reconnect(server_id)
                                logger.info("daemon_mcp_health_reconnect_ok server=%s", server_id)
                                reconnected = True
                            except MCPTransportError as exc:
                                logger.error("daemon_mcp_health_reconnect_fail server=%s: %s", server_id, exc)
                    except Exception as exc:
                        logger.debug("daemon_mcp_health_error server=%s: %s", server_id, exc)

                if reconnected:
                    await self._emit(MCPServerEvent.CONNECTED, server_id)

    async def health_check_all(self) -> dict[str, bool]:
        """Run health check on all connected servers. Returns {server_id: ok}."""
        results: dict[str, bool] = {}
        for server_id in list(self._refs.keys()):
            entry = self._pool.get_server(server_id)
            if entry is None:
                results[server_id] = False
                continue
            try:
                results[server_id] = await self._pool.ping(server_id)
            except Exception:
                results[server_id] = False
        return results
