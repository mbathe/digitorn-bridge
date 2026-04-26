---
id: module-concept-mcp
title: "mcp module — overview"
type: module-concept
module: mcp
isolation: shared
keywords: [mcp, mcp-module, connect, disconnect, reconnect, list_servers, list_tools, call_tool, list_resources, read_resource, list_prompts, get_prompt, health_check]
version: 1.0.0
---

# `mcp` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 11 visible, 0 internal

## Description (from class docstring)

MCPModule — MCP server integration for Digitorn agents.

Connects to MCP (Model Context Protocol) servers and exposes their
tools, resources, and prompts to Digitorn agents.  MCP tools are
indexed alongside native tools in the context_builder's ToolIndex
for seamless discovery and execution.

Supports all MCP transports:
- stdio: subprocess via stdin/stdout (most common)
- SSE: HTTP Server-Sent Events
- Streamable HTTP: HTTP POST with optional streaming

Each app gets its own MCPModule instance (per-app isolation via
registry.create()), so MCP server connections don't leak between apps.

> Class-level summary: MCP server integration module.

    Manages connections to MCP servers and exposes their tools
    to the Digitorn agent system via the context_builder's ToolIndex.

    The module itself has management actions (connect, disconnect, etc.)
    while MCP tools are indexed as virtual tools with FQN
    ``mcp_{server_id}.{tool_name}`` and routed through this module's
    ``execute()`` method.

## Configuration

Set under `modules.mcp.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon. |
| `servers` | dict |  | `{}` | Named MCP server configs (free-form per transport). |
| `cache` | dict |  | `{}` |  |
| `middleware` | list |  | `[]` |  |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `connect` | `McpConnect` |  | medium | Connect to an MCP server (stdio subprocess, SSE, or HTTP) |
| `disconnect` | `McpDisconnect` |  | low | Disconnect from an MCP server |
| `reconnect` | `McpReconnect` |  | medium | Reconnect a failed MCP server |
| `list_servers` | `McpListServers` |  | low | List all connected MCP servers and their status |
| `list_tools` | `McpListTools` |  | low | List all tools exposed by a specific MCP server |
| `call_tool` | `McpCallTool` |  | medium | Call a tool on a specific MCP server |
| `list_resources` | `McpListResources` |  | low | List resources from an MCP server |
| `read_resource` | `McpReadResource` |  | low | Read a resource from an MCP server |
| `list_prompts` | `McpListPrompts` |  | low | List prompt templates from an MCP server |
| `get_prompt` | `McpGetPrompt` |  | low | Get a prompt template from an MCP server with arguments filled in |
| `health_check` | `McpHealthCheck` |  | low | Health check one or all MCP servers |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: mcp
      actions: [connect, disconnect, reconnect, list_servers, list_tools, call_tool, list_resources, read_resource, list_prompts, get_prompt, health_check]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {mcp: [connect, disconnect, reconnect, list_servers, list_tools]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/mcp-*.md`.
