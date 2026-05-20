"""MCPConnectionPool - manages named MCP server connections."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from digitorn.modules.mcp.protocol import (
    MCPPromptDef,
    MCPResourceDef,
    MCPToolDef,
    MCPToolResult,
    build_prompts_get,
    build_prompts_list,
    build_resources_list,
    build_resources_read,
    build_tools_call,
    build_tools_list,
    parse_prompts_list,
    parse_resources_list,
    parse_tool_result,
    parse_tools_list,
)
from digitorn.modules.mcp.transports import (
    MCPTransport,
    MCPTransportError,
    create_transport,
)

logger = logging.getLogger(__name__)

@dataclass
class MCPServerEntry:
    """A connected MCP server with cached capabilities."""

    server_id: str
    transport_type: str
    transport: MCPTransport
    tools: list[MCPToolDef] = field(default_factory=list)
    resources: list[MCPResourceDef] = field(default_factory=list)
    prompts: list[MCPPromptDef] = field(default_factory=list)
    status: str = "connected"
    auth_config: Any | None = None
    created_at: float = field(default_factory=time.time)
    last_ping: float | None = None
    error: str | None = None
    _connect_kwargs: dict[str, Any] = field(default_factory=dict)
    tool_examples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _consecutive_failures: int = 0

    @property
    def server_info(self) -> dict[str, Any]:
        return getattr(self.transport, "server_info", {})

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return getattr(self.transport, "server_capabilities", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "transport_type": self.transport_type,
            "status": self.status,
            "server_info": self.server_info,
            "tools_count": len(self.tools),
            "resources_count": len(self.resources),
            "prompts_count": len(self.prompts),
            "created_at": self.created_at,
            "last_ping": self.last_ping,
            "error": self.error,
            "auth_type": (
                "oauth2" if self.auth_config is not None else None
            ),
        }

class MCPConnectionPool:
    """Manages named MCP server connections."""

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerEntry] = {}
        self._lock = asyncio.Lock()  # Serialize connect/disconnect/reconnect

    @property
    def servers(self) -> dict[str, MCPServerEntry]:
        return self._servers

    async def connect(
        self,
        server_id: str,
        transport_type: str,
        **kwargs: Any,
    ) -> MCPServerEntry:
        """Connect to an MCP server."""
        async with self._lock:
            if server_id in self._servers:
                await self._disconnect_unlocked(server_id)

            transport = create_transport(transport_type, **kwargs)
            entry = MCPServerEntry(
                server_id=server_id,
                transport_type=transport_type,
                transport=transport,
                _connect_kwargs={"transport_type": transport_type, **kwargs},
            )

            try:
                await transport.connect()
                entry.status = "connected"

                await self._refresh_capabilities(entry)

                logger.info(
                    "mcp_server_connected id=%s tools=%d resources=%d prompts=%d",
                    server_id, len(entry.tools), len(entry.resources), len(entry.prompts),
                )

            except MCPTransportError as exc:
                entry.status = "error"
                entry.error = str(exc)
                logger.error("mcp_connect_failed id=%s error=%s", server_id, exc)
                self._servers[server_id] = entry
                raise

            self._servers[server_id] = entry
            return entry

    async def disconnect(self, server_id: str) -> None:
        """Disconnect from an MCP server."""
        async with self._lock:
            await self._disconnect_unlocked(server_id)

    async def _disconnect_unlocked(self, server_id: str) -> None:
        entry = self._servers.pop(server_id, None)
        if entry is None:
            return

        try:
            await entry.transport.close()
        except Exception:
            logger.debug("mcp_disconnect_error id=%s", server_id, exc_info=True)

        entry.status = "disconnected"
        logger.info("mcp_server_disconnected id=%s", server_id)

    async def disconnect_all(self) -> int:
        """Disconnect all servers. Returns count of disconnected servers."""
        async with self._lock:
            server_ids = list(self._servers.keys())
            for sid in server_ids:
                await self._disconnect_unlocked(sid)
            return len(server_ids)

    async def reconnect(
        self, server_id: str, *, max_retries: int | None = None, base_delay: float = 1.0, max_delay: float = 30.0,
    ) -> MCPServerEntry:
        """Reconnect a server using its original config with exponential backoff."""
        if max_retries is None:
            try:
                from digitorn.core.config import get_settings
                max_retries = get_settings().mcp.max_reconnect_attempts
            except Exception:
                max_retries = 4
        async with self._lock:
            entry = self._servers.get(server_id)
            if entry is None:
                raise MCPTransportError(f"Unknown server: {server_id}")

            try:
                await entry.transport.close()
            except Exception as exc:
                logger.debug("connections best-effort block failed: %s", exc)

            kwargs = entry._connect_kwargs.copy()
            transport_type = kwargs.pop("transport_type")

            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                if attempt > 0:
                    import random
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    jitter = random.uniform(0, delay * 0.2)  # 20% jitter
                    delay += jitter
                    logger.info(
                        "mcp_reconnect_backoff id=%s attempt=%d delay=%.1fs",
                        server_id, attempt, delay,
                    )
                    await asyncio.sleep(delay)

                transport = create_transport(transport_type, **kwargs)
                try:
                    await transport.connect()
                    # Store new transport in a temporary entry for capability refresh.
                    # Only commit to entry.transport AFTER capabilities refresh succeeds,
                    # to avoid leaving entry in a half-updated state on refresh failure.
                    old_transport = entry.transport
                    entry.transport = transport
                    try:
                        await self._refresh_capabilities(entry)
                    except Exception:
                        # Refresh failed - restore old transport and re-raise so this
                        # attempt counts as failed.
                        entry.transport = old_transport
                        try:
                            await transport.close()
                        except Exception as exc:
                            logger.debug("connections best-effort block failed: %s", exc)
                        raise
                    entry.status = "connected"
                    entry.error = None
                    entry.created_at = time.time()
                    entry._consecutive_failures = 0
                    logger.info(
                        "mcp_server_reconnected id=%s tools=%d attempts=%d",
                        server_id, len(entry.tools), attempt + 1,
                    )
                    return entry
                except MCPTransportError as exc:
                    last_exc = exc
                    entry._consecutive_failures += 1
                    logger.warning(
                        "mcp_reconnect_attempt_failed id=%s attempt=%d error=%s",
                        server_id, attempt + 1, exc,
                    )

            # All retries exhausted
            entry.status = "error"
            entry.error = str(last_exc)
            logger.error(
                "mcp_reconnect_exhausted id=%s attempts=%d error=%s",
                server_id, max_retries + 1, last_exc,
            )
            raise last_exc  # type: ignore[misc]

    async def call_tool(
        self, server_id: str, tool_name: str, arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> MCPToolResult:
        """Call a tool on an MCP server with timeout protection."""
        if timeout is None:
            try:
                from digitorn.core.config import get_settings
                timeout = get_settings().mcp.tool_call_timeout
            except Exception:
                timeout = 120.0
        entry = self._get_connected(server_id)

        try:
            result = await asyncio.wait_for(
                entry.transport.send("tools/call", {
                    "name": tool_name,
                    **({"arguments": arguments} if arguments is not None else {}),
                }),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise MCPTransportError(
                f"MCP tool '{tool_name}' on '{server_id}' timed out after {timeout}s"
            )
        return parse_tool_result(result)

    async def list_resources(self, server_id: str) -> list[MCPResourceDef]:
        """List resources from an MCP server."""
        entry = self._get_connected(server_id)
        result = await entry.transport.send("resources/list")
        return parse_resources_list(result)

    async def read_resource(self, server_id: str, uri: str) -> Any:
        """Read a resource from an MCP server."""
        entry = self._get_connected(server_id)
        result = await entry.transport.send("resources/read", {"uri": uri})
        return result

    async def list_prompts(self, server_id: str) -> list[MCPPromptDef]:
        """List prompts from an MCP server."""
        entry = self._get_connected(server_id)
        result = await entry.transport.send("prompts/list")
        return parse_prompts_list(result)

    async def get_prompt(
        self, server_id: str, name: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        """Get a prompt from an MCP server."""
        entry = self._get_connected(server_id)
        params: dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments
        return await entry.transport.send("prompts/get", params)

    async def ping(self, server_id: str, *, timeout: float = 10.0) -> bool:
        """Ping an MCP server. Returns True if alive."""
        entry = self._servers.get(server_id)
        if entry is None or not entry.transport.connected:
            return False

        try:
            await asyncio.wait_for(entry.transport.send("ping"), timeout=timeout)
            entry.last_ping = time.time()
            entry.status = "connected"
            entry.error = None
            return True
        except (MCPTransportError, asyncio.TimeoutError) as exc:
            entry.status = "error"
            entry.error = str(exc) if not isinstance(exc, asyncio.TimeoutError) else "ping timeout"
            return False

    async def refresh_capabilities(self, server_id: str) -> MCPServerEntry:
        """Re-fetch tools/resources/prompts from a server."""
        entry = self._get_connected(server_id)
        await self._refresh_capabilities(entry)
        return entry

    def get_server(self, server_id: str) -> MCPServerEntry | None:
        return self._servers.get(server_id)

    def list_servers(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self._servers.values()]

    def get_all_tools(self) -> list[tuple[str, MCPToolDef]]:
        """Return all tools across all connected servers as (server_id, tool) pairs."""
        result = []
        for entry in self._servers.values():
            if entry.status == "connected":
                for tool in entry.tools:
                    result.append((entry.server_id, tool))
        return result

    def _get_connected(self, server_id: str) -> MCPServerEntry:
        entry = self._servers.get(server_id)
        if entry is None:
            raise MCPTransportError(f"Server not connected: {server_id}")
        if entry.status != "connected" or not entry.transport.connected:
            raise MCPTransportError(
                f"Server '{server_id}' is not connected (status={entry.status})"
            )
        return entry

    async def _refresh_capabilities(self, entry: MCPServerEntry) -> None:
        caps = entry.server_capabilities

        _CAP_TIMEOUT = 15.0  # Max time per capability category

        if caps.get("tools") is not None or not caps:
            try:
                result = await asyncio.wait_for(
                    entry.transport.send("tools/list"), timeout=_CAP_TIMEOUT,
                )
                entry.tools = parse_tools_list(result)
            except (MCPTransportError, asyncio.TimeoutError) as exc:
                logger.warning("mcp_tools_list_failed id=%s: %s", entry.server_id, exc)

        if caps.get("resources") is not None:
            try:
                result = await asyncio.wait_for(
                    entry.transport.send("resources/list"), timeout=_CAP_TIMEOUT,
                )
                entry.resources = parse_resources_list(result)
            except (MCPTransportError, asyncio.TimeoutError) as exc:
                logger.debug("mcp_resources_list_failed id=%s: %s", entry.server_id, exc)

        if caps.get("prompts") is not None:
            try:
                result = await asyncio.wait_for(
                    entry.transport.send("prompts/list"), timeout=_CAP_TIMEOUT,
                )
                entry.prompts = parse_prompts_list(result)
            except (MCPTransportError, asyncio.TimeoutError) as exc:
                logger.debug("mcp_prompts_list_failed id=%s: %s", entry.server_id, exc)
