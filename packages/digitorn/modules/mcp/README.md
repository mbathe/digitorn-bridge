# MCP Module

Connect to external MCP (Model Context Protocol) servers and expose their
tools, resources, and prompts to Digitorn agents — seamlessly integrated
with the context_builder's ToolIndex.

## Overview

The MCP module bridges Digitorn agents with any MCP-compatible server.
Connected servers' tools appear as virtual modules (`mcp_slack`,
`mcp_github`, etc.) in the ToolIndex, discoverable and executable
exactly like native tools. Security policies (grant/approve/deny)
apply identically to MCP virtual modules.

Supports all three MCP transports:
- **stdio** — subprocess via stdin/stdout JSON-RPC (most common)
- **SSE** — HTTP Server-Sent Events
- **Streamable HTTP** — HTTP POST with optional streaming

## Actions

### Lifecycle

| Action | Description | Risk | Side Effects |
|--------|-------------|------|--------------|
| `connect` | Connect to an MCP server | Medium | `subprocess_spawn`, `network_connection` |
| `disconnect` | Disconnect from an MCP server | Low | — |
| `reconnect` | Reconnect a failed MCP server | Medium | `subprocess_spawn`, `network_connection` |
| `list_servers` | List all connected servers and status | Low | — |
| `health_check` | Health check one or all servers | Low | — |

### Tools

| Action | Description | Risk | Side Effects |
|--------|-------------|------|--------------|
| `list_tools` | List tools exposed by a server | Low | — |
| `call_tool` | Call a tool on a specific server | Medium | `external_api_call` |

### Resources

| Action | Description | Risk | Side Effects |
|--------|-------------|------|--------------|
| `list_resources` | List resources from a server | Low | — |
| `read_resource` | Read a resource by URI | Low | — |

### Prompts

| Action | Description | Risk | Side Effects |
|--------|-------------|------|--------------|
| `list_prompts` | List prompt templates from a server | Low | — |
| `get_prompt` | Get a prompt with arguments filled in | Low | — |

## Configuration

### Daemon-managed (recommended)

Install, configure, and test servers via the CLI, then reference by name:

```bash
digitorn mcp install github
digitorn mcp config github --set token=ghp_xxxxx
digitorn mcp test github
```

```yaml
modules:
  mcp:
    servers:
      - github
      - slack
```

### Shorthand (catalog)

The built-in catalog auto-resolves command, args, transport, and env mapping:

```yaml
modules:
  mcp:
    servers:
      github:
        token: "{{secret.GITHUB_TOKEN}}"
      brave_search:
        api_key: "{{secret.BRAVE_API_KEY}}"
      memory: {}
```

### Explicit (full control)

```yaml
modules:
  mcp:
    servers:
      custom_api:
        transport: sse
        url: "http://localhost:3000/sse"
        headers:
          Authorization: "Bearer {{env.API_TOKEN}}"
    constraints:
      allowed_servers: [custom_api]  # optional restriction
```

Servers declared under `servers:` are auto-connected at deploy time via
`on_config_update()`. Agents can also connect to new servers at runtime
via the `connect` action (hot-reload: new tools appear immediately).

## Server Sources

Servers are resolved in order:

1. **Internal catalog** — ~30 pre-configured servers (github, slack, notion, stripe, etc.)
2. **Remote registry** — `registry.modelcontextprotocol.io` (~800 servers)
3. **Smithery** — hosted servers via Smithery Connect API (`via: smithery`)
4. **Custom** — user provides command/url/transport explicitly

At install time, Digitorn **probes the server's source code** to discover
the actual env var names it reads (`process.env.XXX` in JS,
`os.environ.get("XXX")` in Python). This corrects mismatches between
registry metadata and actual server code. Required npm/pip packages are
auto-installed if missing.

## Transports

| Transport | Config Keys | Use Case |
|-----------|------------|----------|
| `stdio` | `command`, `args`, `env` | Local MCP servers (npx, Docker, binaries) |
| `sse` | `url`, `headers` | Remote MCP servers with SSE streaming |
| `streamable_http` | `url`, `headers` | Remote MCP servers with HTTP POST streaming |

All transports support auto-reconnect with exponential backoff (1s → 30s max)
and configurable `timeout` (default 30s).

## Virtual Module IDs

Each MCP server becomes a virtual module with FQN `mcp_{server_id}.{tool_name}`:

```
list_categories → ["filesystem", "database", "mcp_slack", "mcp_github"]
browse_category("mcp_slack") → [mcp_slack.post_message, mcp_slack.list_channels, ...]
```

The `mcp_` prefix prevents collisions with native modules. Virtual tools are
indexed in both keyword and semantic search indexes.

## Security

MCP virtual modules pass through the same 5-gate security enforcement as
native modules:

```yaml
capabilities:
  grant:
    - module: mcp_slack
      actions: [list_channels, post_message]
  approve:
    - module: mcp_github
      actions: [create_issue]
  deny:
    - module: mcp_github
      actions: [delete_repository]
```

- Default risk level for MCP tools: **medium** (external API calls)
- Subprocess stdio: only env vars declared in YAML config are injected
- `mcp_*` virtual module IDs skip compile-time validation (resolved at runtime)

## OAuth2 — Per-User Authentication

MCP servers that require user-level tokens (Google Calendar, GitHub user-scope, etc.)
are supported via OAuth2 Authorization Code flow with optional PKCE (S256).

### Configuration

Add an `auth:` block to the server config:

```yaml
servers:
  google_calendar:
    transport: sse
    url: "http://localhost:3000/sse"
    auth:
      type: oauth2
      provider: google
      client_id: "{{env.GOOGLE_CLIENT_ID}}"
      client_secret: "{{env.GOOGLE_CLIENT_SECRET}}"
      scopes: ["https://www.googleapis.com/auth/calendar.readonly"]
```

### Well-Known Providers

| Provider | PKCE | Auto-configured URLs |
|----------|------|---------------------|
| `google` | Yes (S256) | accounts.google.com |
| `github` | No | github.com |
| `slack` | No | slack.com |
| `microsoft` | Yes (S256) | login.microsoftonline.com |
| `notion` | No | api.notion.com (Basic auth + JSON body) |
| `custom` | No | Requires explicit `authorize_url` + `token_url` |

For stdio servers, tokens are injected via `env_token_var` (environment variable)
instead of HTTP headers. The subprocess is restarted with the new token.

### Flow

1. Agent calls MCP tool → MCPModule checks for auth config
2. If no valid token → returns `requires_oauth` error with `auth_url`
3. User authorizes → callback exchanges code for tokens (stored encrypted via Fernet)
4. Agent retries → token injected into transport `Authorization` header
5. Tokens are auto-refreshed 5 minutes before expiry

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/apps/{app_id}/oauth/authorize` | GET | Start OAuth flow (params: `provider`, `server_id`, `session_id`) |
| `/api/apps/{app_id}/oauth/callback` | GET | Provider callback (params: `code`, `state`) |

### Key Files

- `oauth.py` — `OAuthManager`, `OAuthProviderConfig`, PKCE generation, token exchange
- `module.py` — `_ensure_oauth_token()`, token injection into transport headers
- `core/app/users.py` — `UserStore.refresh_token_if_needed()`, encrypted token storage

## Smithery Integration

Servers hosted on [Smithery](https://smithery.ai/) can be accessed via
`via: smithery` in YAML config:

```yaml
servers:
  github:
    via: smithery
    smithery_key: "{{secret.SMITHERY_API_KEY}}"
```

The catalog maps known server IDs to Smithery slugs automatically.

## Schema Probing

At connection time, the module can probe MCP servers to discover the actual
JSON structures they expect (critical for LLMs without built-in API knowledge).
The probe calls a few read tools, extracts templates, and injects them into
the agent's system prompt. See `schema_probe.py`.

## SDK Fix Wrapper

`sdk_fix_wrapper.py` patches a bug in the MCP Python SDK where `Optional[str]`
parameters are incorrectly converted from JSON strings to dicts. Python MCP
servers are automatically launched through this wrapper.

## Requirements

- `asyncio` (stdlib — subprocess management, async transports)
- `httpx` (SSE, HTTP transports, OAuth token exchange)
- `cryptography` (Fernet token encryption — optional, base64 fallback)

## Platform Support

| Platform | Status |
|----------|--------|
| Linux | Supported |
| macOS | Supported |
| Windows | Supported (stdio + HTTP; SSE may vary) |
