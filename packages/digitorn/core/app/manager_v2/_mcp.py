"""_McpMixin - MCP server event handling."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ._models import DeployedApp

if TYPE_CHECKING:
    from digitorn.core.mcp_pool import MCPServerEvent  # noqa: F401

logger = logging.getLogger(__name__)


class _McpMixin:
    """Reactions to MCP daemon-pool server lifecycle events."""

    _deployed: dict[str, DeployedApp]

    async def _on_mcp_event(
        self, event: "MCPServerEvent", server_id: str,
    ) -> None:
        """Called by daemon pool when a server changes state.

        Handles:
        - CONNECTED: rebuild tool index (tools may have changed)
        - DISCONNECTED: rebuild tool index (tools removed)
        - CONFIG_UPDATED: reconnect server with new config, then rebuild
        """
        from digitorn.core.mcp_pool import MCPServerEvent

        if event == MCPServerEvent.CONFIG_UPDATED:
            await self._handle_mcp_config_updated(server_id)
            return

        # Snapshot to avoid "dict changed during iteration" if undeploy races
        for app_id, deployed in list(self._deployed.items()):
            mcp_module = deployed.modules.get("mcp")
            if mcp_module is None:
                continue
            if server_id not in getattr(mcp_module, "_daemon_server_ids", set()):
                continue
            await self._rebuild_app_tool_index(app_id, deployed, server_id, event.value)

    async def _handle_mcp_config_updated(self, server_id: str) -> None:
        """Reconnect a daemon-managed server after its config changed in DB."""
        pool = getattr(self, "_daemon_mcp_pool", None)
        if pool is None:
            return

        entry = pool.get_server(server_id)
        if entry is None:
            return

        try:
            from digitorn.core.mcp_store import (
                get_server as db_get_server,
                _build_connect_kwargs,
            )

            async with pool._session_factory() as session:
                server = await db_get_server(session, server_id)
            if server is None:
                return

            kwargs = _build_connect_kwargs(server)
            await pool._pool.disconnect(server_id)
            await pool._pool.connect(server_id, server.transport, **kwargs)
            logger.info("mcp_config_reconnect_ok server=%s", server_id)

            # Snapshot to avoid "dict changed during iteration" if undeploy races
            for app_id, deployed in list(self._deployed.items()):
                mcp_module = deployed.modules.get("mcp")
                if mcp_module is None:
                    continue
                if server_id not in getattr(mcp_module, "_daemon_server_ids", set()):
                    continue
                await self._rebuild_app_tool_index(app_id, deployed, server_id, "config_updated")

        except Exception as exc:
            logger.error("mcp_config_reconnect_fail server=%s: %s", server_id, exc, exc_info=True)

    async def _rebuild_app_tool_index(
        self,
        app_id: str,
        deployed: DeployedApp,
        server_id: str,
        reason: str,
    ) -> None:
        """Rebuild tool index for a single deployed app.

        Async because ``cb.build_and_set_index`` walks fastembed/ONNX
        over every tool description -- 2-5s of CPU work that we MUST
        off-load to a worker thread or the event loop stalls long
        enough to drop Socket.IO heartbeats and trip the watchdog.
        """
        cb = deployed.context_builder
        if cb is None:
            return

        old_count = cb.index.total_tools if cb.index else 0
        security_profile = getattr(deployed.compiled, "security_profile", None)
        new_index = await asyncio.to_thread(
            cb.build_and_set_index, deployed.modules, security_profile,
        )
        new_count = new_index.total_tools if new_index else 0

        if new_count != old_count:
            self._refresh_agent_tools(
                deployed.compiled,
                {"contexts": deployed.contexts},
                cb,
                new_index,
            )
            logger.info(
                "tool_index_rebuilt app=%s server=%s reason=%s tools=%d→%d",
                app_id, server_id, reason, old_count, new_count,
            )
