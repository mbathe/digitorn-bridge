# MCP Module — Actions Reference

Complete reference for all actions exposed by the MCP module.

The MCP module connects to external MCP (Model Context Protocol) servers and
exposes their tools, resources, and prompts to Digitorn agents. Each connected
server becomes a virtual module (`mcp_slack`, `mcp_github`, etc.) whose tools
are indexed and executable like native tools.

---

## Lifecycle

### `connect`

Connect to an MCP server via stdio subprocess, SSE, or HTTP.

**Permissions:** — · **Risk level:** Medium
**Side effects:** `subprocess_spawn`, `network_connection`

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | yes | — | Unique name for this server (e.g. `"slack"`, `"github"`). 1–128 chars. |
| `transport` | string | no | `"stdio"` | Transport type: `"stdio"`, `"sse"`, or `"streamable_http"`. |
| `command` | string | no | null | Command to run (stdio only, e.g. `"npx"`, `"python"`). |
| `args` | list[string] | no | `[]` | Command arguments (stdio only). |
| `env` | dict | no | `{}` | Environment variables for subprocess (stdio only). Blocked keys are filtered. |
| `url` | string | no | null | Server URL (sse/http only). |
| `headers` | dict | no | `{}` | HTTP headers (sse/http only). |
| `timeout` | float | no | `30.0` | Request timeout in seconds (1–300). |

#### Returns

Server entry dict with `server_id`, `status`, `tools_count`, `resources_count`, `prompts_count`.

---

### `disconnect`

Disconnect from an MCP server. Closes the transport and removes from the pool.

**Permissions:** — · **Risk level:** Low

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | yes | — | Server ID to disconnect. |

---

### `reconnect`

Reconnect a failed MCP server using its original connection config.

**Permissions:** — · **Risk level:** Medium
**Side effects:** `subprocess_spawn`, `network_connection`

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | yes | — | Server ID to reconnect. |

---

### `list_servers`

List all connected MCP servers and their status.

**Permissions:** — · **Risk level:** Low

#### Parameters

None.

#### Returns

```json
{
  "servers": [
    {"server_id": "slack", "status": "connected", "tools_count": 12, ...},
    {"server_id": "github", "status": "error", "error": "timeout", ...}
  ],
  "count": 2
}
```

---

### `health_check`

Ping one or all MCP servers to check connectivity.

**Permissions:** — · **Risk level:** Low

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | no | null | Server to check. Omit for all servers. |

---

## Tools

### `list_tools`

List all tools exposed by a specific MCP server.

**Permissions:** — · **Risk level:** Low

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | yes | — | Server ID to list tools from. |

#### Returns

```json
{
  "server_id": "slack",
  "tools": [
    {"name": "post_message", "description": "Post a message", "input_schema": {...}},
    {"name": "list_channels", "description": "List channels", "input_schema": {...}}
  ]
}
```

---

### `call_tool`

Call a tool on a specific MCP server with arguments.

**Permissions:** — · **Risk level:** Medium
**Side effects:** `external_api_call`

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | yes | — | Server ID where the tool lives. |
| `tool_name` | string | yes | — | Name of the tool to call. 1–256 chars. |
| `arguments` | dict | no | `{}` | Arguments to pass to the tool. |

#### Returns

```json
{
  "content": [{"type": "text", "text": "Message posted successfully"}],
  "text": "Message posted successfully"
}
```

---

## Resources

### `list_resources`

List resources exposed by an MCP server.

**Permissions:** — · **Risk level:** Low

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | yes | — | Server ID to list resources from. |

#### Returns

```json
{
  "server_id": "github",
  "resources": [
    {"uri": "github://repo/main/README.md", "name": "README.md", "description": "...", "mime_type": "text/markdown"}
  ]
}
```

---

### `read_resource`

Read a specific resource from an MCP server by URI.

**Permissions:** — · **Risk level:** Low

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | yes | — | Server ID where the resource lives. |
| `uri` | string | yes | — | Resource URI to read. 1–2048 chars. |

---

## Prompts

### `list_prompts`

List prompt templates from an MCP server.

**Permissions:** — · **Risk level:** Low

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | yes | — | Server ID to list prompts from. |

#### Returns

```json
{
  "server_id": "github",
  "prompts": [
    {
      "name": "summarize_pr",
      "description": "Summarize a pull request",
      "arguments": [{"name": "pr_number", "description": "PR number", "required": true}]
    }
  ]
}
```

---

### `get_prompt`

Get a prompt template from an MCP server with arguments filled in.

**Permissions:** — · **Risk level:** Low

#### Parameters

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `server_id` | string | yes | — | Server ID where the prompt lives. |
| `prompt_name` | string | yes | — | Name of the prompt template. 1–256 chars. |
| `arguments` | dict | no | `{}` | Arguments to fill into the template. |

---

## Virtual Tool Routing

When MCP servers are connected, their tools are indexed in the `ToolIndex` with
FQN `mcp_{server_id}.{tool_name}`. Agents can discover and call these tools
identically to native module tools.

When the agent calls `execute_tool(name="mcp_slack.post_message", params={...})`,
the context_builder routes it to `MCPModule.execute("mcp_slack__post_message", ...)`,
which parses the server_id and tool_name, then delegates to `pool.call_tool("slack", "post_message", ...)`.

The regex `^mcp_([^_]+(?:_[^_]+)*)__(.+)$` handles compound server IDs like
`mcp_google_calendar__list_events` → server_id=`google_calendar`, tool_name=`list_events`.

---

## OAuth Error Response

When a server has an `auth:` config and the user has no valid token, virtual tool
calls return a special error with OAuth metadata:

```json
{
  "success": false,
  "error": "User needs to authorize google for server 'google_calendar'. Open the authorization URL to proceed.",
  "metadata": {
    "requires_oauth": true,
    "provider": "google",
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=...&scope=...&state=...",
    "state": "abc123...",
    "server_id": "google_calendar"
  }
}
```

The agent can present the `auth_url` to the user. Once authorized, retrying the
tool call will succeed (token is auto-injected into the transport headers).
