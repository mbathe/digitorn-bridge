---
id: mcp
title: MCP Module
sidebar_label: mcp
sidebar_position: 7
description: Connect external MCP servers with result normalization, smart cache, middleware pipeline, and auto-reconnect.
---

# mcp

Connect and manage external Model Context Protocol (MCP) servers. The module normalizes raw MCP outputs into clean structured results, provides smart caching, middleware pipelines, and automatic reconnection on failure.

| Property | Value |
|----------|-------|
| **Module ID** | `mcp` |
| **Version** | `1.0.0` |
| **Type** | system |
| **Dependencies** | MCP SDK (`mcp` package) |

---

## Design Philosophy

- **Plug and play** - 60+ pre-configured servers in the built-in catalog. Install with `digitorn mcp install {name}`.
- **Result normalization** - raw MCP Content items are transformed into clean ActionResult with `status`, `output`, `result_count`. The LLM never sees confusing raw data.
- **Smart cache** - explicitly whitelisted MCP tools are cached per server via `cacheable_tools`. Only metadata/static tools should be cached - live data (emails, issues) changes externally.
- **Resilient** - auto-reconnect on transport errors. Circuit breaker pattern for failing servers.
- **Secure** - environment variable filtering, credential file permissions, result source marking.

---

## Configuration

```yaml
modules:
  mcp:
    servers:
      github:
        command: "mcp-server-github"
        env:
          GITHUB_TOKEN: "{{env.GITHUB_TOKEN}}"
      memory:
        command: "mcp-server-memory"
    config:
      cache:
        ttl: 300
        max_size: 200
      servers:
        github:
          cacheable_tools: [list_repos, get_repo, get_file_contents]
```
---

## Actions (11)

### connect
Connect to an MCP server (stdio, SSE, or HTTP). **Risk: medium**

### disconnect
Disconnect from an MCP server. **Risk: low**

### reconnect
Reconnect a failed server. **Risk: medium**

### list_servers
List all connected servers and their status. **Risk: low**

### list_tools
List all tools exposed by a specific server. **Risk: low**

### call_tool
Call a tool on a specific server. **Risk: medium**

### list_resources
List resources from a server. **Risk: low**

### read_resource
Read a resource from a server. **Risk: low**

### list_prompts
List prompt templates from a server. **Risk: low**

### get_prompt
Get a prompt template with arguments filled in. **Risk: low**

### health_check
Health check one or all servers. **Risk: low**

---

## Constraints

| Constraint | Type | Description |
|------------|------|-------------|
| `allowed_servers` | string_list | Restrict which MCP servers can be used. |

### Example App YAML

```yaml
modules:
  - module: mcp
    constraints:
      allowed_servers: [github, memory]
```
---

## Virtual Tools

When MCP servers are connected, their tools are automatically indexed in the context builder as virtual tools. The agent discovers them via `search_tools` and executes them via `execute_tool`, just like native module actions. The routing is transparent.
