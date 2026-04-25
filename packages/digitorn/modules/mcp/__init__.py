"""MCP module — Model Context Protocol server integration.

Connects to MCP servers (stdio, SSE, HTTP) and exposes their tools,
resources, and prompts to Digitorn agents via the context_builder's
ToolIndex.

MCP tools appear as virtual tools with FQN ``mcp_{server_id}.{tool_name}``
and are indexed alongside native tools for seamless discovery.
"""

from digitorn.modules.mcp.module import MCPModule

__all__ = ["MCPModule"]
