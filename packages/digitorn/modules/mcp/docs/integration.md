# MCP Module — Integration Guide

## YAML Configuration

### Three configuration styles

Digitorn supports three ways to declare MCP servers, from simplest to most
explicit. All three can be mixed in the same YAML file.

#### 1. Daemon-managed (recommended)

Servers installed via `digitorn mcp install` are referenced by name only.
The daemon already has all connection details, credentials, and transport config.

```yaml
modules:
  mcp:
    config:
      servers:
        - github
        - slack
        - brave_search
```

#### 2. Shorthand (catalog)

The built-in catalog (~15 servers) and remote registry (~800 servers)
auto-resolve command, args, transport, and env mapping from just a name +
credentials:

```yaml
modules:
  mcp:
    config:
      servers:
        github:
          token: "{{secret.GITHUB_TOKEN}}"
        brave_search:
          api_key: "{{secret.BRAVE_API_KEY}}"
        filesystem:
          path: "{{workspace}}"
        memory: {}
```

The catalog translates shorthand keys to real env var names. For example,
`token` → `GITHUB_PERSONAL_ACCESS_TOKEN` for the GitHub server.

#### 3. Explicit (full control)

Full control over transport, command, args, env, and headers. Bypasses
both catalog and registry:

```yaml
modules:
  mcp:
    config:
      servers:
        slack:
          transport: stdio
          command: npx
          args: ["@modelcontextprotocol/server-slack"]
          env:
            SLACK_BOT_TOKEN: "{{env.SLACK_BOT_TOKEN}}"
            SLACK_TEAM_ID: "{{env.SLACK_TEAM_ID}}"
        custom_api:
          transport: sse
          url: "http://localhost:3000/sse"
          headers:
            Authorization: "Bearer {{env.API_TOKEN}}"
          timeout: 60
        remote_service:
          transport: streamable_http
          url: "https://mcp.example.com/v1"
          headers:
            X-API-Key: "{{env.SERVICE_KEY}}"
      constraints:
        allowed_servers: [slack, custom_api]
```

## Server Resolution

When processing a server entry, the module resolves configuration in order:

1. **Explicit config** — if `command`, `url`, or `transport` is present → used as-is
2. **Internal catalog** — ~30 pre-configured servers (github, slack, notion, stripe, etc.)
3. **Remote registry** — `registry.modelcontextprotocol.io` (~800 servers)
4. **Smithery** — hosted servers via Smithery Connect API (`via: smithery`)
5. **Source code probe** — after install, scans the server's source to discover
   actual env var names (`process.env.XXX` in JS, `os.environ.get("XXX")` in Python)

The probe system ensures credentials are always injected with the correct
env var name, even when registry metadata is inaccurate.

## CLI Management

### Install & configure

```bash
# Catalog server (auto-resolved)
digitorn mcp install github

# Registry server (auto-discovered + probed)
digitorn mcp install todoist-mcp

# Custom local server
digitorn mcp install my-server \
  --command python3 --args "-m,my_mcp_server"

# Custom remote server
digitorn mcp install my-api \
  --url "http://localhost:8080/mcp" --transport streamable_http

# Configure credentials
digitorn mcp config github --set token=ghp_xxxxx

# Show required credentials before install
digitorn mcp requirements todoist-mcp

# Test connection
digitorn mcp test github

# List installed servers (with optional filtering)
digitorn mcp list [--status ready] [--json]

# Server details + tools
digitorn mcp info github

# Live daemon pool status
digitorn mcp pool

# Remove
digitorn mcp remove github
```

### OAuth servers

```bash
digitorn mcp install notion
digitorn mcp config notion --oauth
# → Opens browser for OAuth authorization
# → Tokens stored encrypted in ~/.digitorn/mcp/notion/
```

## Auto-Connect

Servers declared under `modules.mcp.config.servers` are automatically connected
at deploy time via `on_config_update()`. The module:

1. Resolves each server config (catalog → registry → explicit)
2. Creates the appropriate transport
3. Performs the MCP initialize handshake
4. Caches the server's tools, resources, and prompts

If a server fails to connect, it's logged as a warning but other servers
proceed normally. The agent can later retry via `mcp.reconnect`.

## Runtime Connection

Agents can also connect to new servers at runtime:

```text
Agent: execute_tool(name="mcp.connect", params={
  "server_id": "brave",
  "transport": "stdio",
  "command": "npx",
  "args": ["@anthropic/mcp-server-brave"],
  "env": {"BRAVE_API_KEY": "..."}
})
```

After connection, the context_builder rebuilds its index and the new server's
tools become immediately discoverable.

## Context Builder Integration

### Virtual Module IDs

Each MCP server appears as a virtual module `mcp_{server_id}`:

```text
list_categories → ["filesystem", "database", "mcp_slack", "mcp_github"]
browse_category("mcp_slack") → [mcp_slack.post_message, mcp_slack.list_channels, ...]
```

### Tool Discovery

MCP tools are indexed in both keyword and semantic indexes alongside native tools.
An agent searching for "post a message" will find `mcp_slack.post_message` just
as easily as any native tool.

### Direct vs Discovery Mode

MCP tools work in both injection modes:

- **Direct mode**: MCP tools appear as OpenAI function schemas alongside native tools
- **Discovery mode**: MCP tools are discoverable via meta-tools (search, browse, get)

The adaptive injection algorithm counts MCP tools in the total when deciding mode.

## Security Integration

### Virtual Module Policies

MCP virtual modules use the same security gates as native modules:

```yaml
capabilities:
  grant:
    - module: mcp_slack
      actions: [list_channels, post_message]
    - module: mcp_filesystem
      actions: [read_file, list_directory]
  approve:
    - module: mcp_github
      actions: [create_issue, create_pull_request]
  deny:
    - module: mcp_github
      actions: [delete_repository]
```

- `mcp_{server_id}` is used as the module ID in policy resolution
- Default risk level for MCP tools: **medium** (external API calls)
- `mcp_*` virtual module IDs skip compile-time validation (resolved at runtime)

### Environment Sanitization

Stdio subprocesses receive a **restricted environment**:

1. **Safe inherited vars**: `PATH`, `HOME`, `USER`, `LANG`, `TERM`, `SHELL`,
   `TMPDIR`, `NODE_PATH`, `PYTHONPATH`, `XDG_*`
2. **Explicit vars from YAML**: Only vars declared in the `env:` block
3. **Blocked vars** (defense in depth): `DATABASE_URL`, `AWS_SECRET_ACCESS_KEY`,
   `DIGITORN_SECRET_KEY`, etc. — blocked even if explicitly declared

### Server Config Validation

`validate_server_config()` checks:

- Transport type is valid (`stdio`, `sse`, `streamable_http`, `http`)
- Required fields present: `command` for stdio, `url` for sse/http

## Transports

| Transport | Wire Protocol | Reconnect | Use Case |
|-----------|--------------|-----------|----------|
| `stdio` | Newline-delimited JSON-RPC over stdin/stdout | Respawn subprocess | Local MCP servers (npx, Docker) |
| `sse` | GET for receiving + POST for sending | Reconnect SSE stream | Remote servers with streaming |
| `streamable_http` | HTTP POST per request | Re-POST | Simple remote servers |

All transports:

- Perform the MCP initialize handshake on connect
- Cache server info and capabilities
- Support configurable timeout (default 30s)
- Handle JSON-RPC 2.0 request/response/notification
- Auto-reconnect with exponential backoff (1s → 30s max)

## Lifecycle

1. **Deploy**: `on_config_update()` auto-connects declared servers
2. **Runtime**: Agent uses tools via `execute_tool` or `call_tool`
3. **Failure**: `reconnect` action retries with original config
4. **Undeploy**: `on_stop()` disconnects all servers, terminates subprocesses

## OAuth2 Per-User Authentication

### Configuration

Servers that need user-level OAuth tokens declare an `auth:` block:

```yaml
modules:
  mcp:
    config:
      servers:
        google_calendar:
          transport: sse
          url: "http://localhost:3000/sse"
          auth:
            type: oauth2
            provider: google
            client_id: "{{env.GOOGLE_CLIENT_ID}}"
            client_secret: "{{env.GOOGLE_CLIENT_SECRET}}"
            scopes:
              - "https://www.googleapis.com/auth/calendar.readonly"
```

For catalog servers with OAuth support (Notion, Google Drive, etc.), the
shorthand form auto-fills `type`, `provider`, and `scopes`:

```yaml
servers:
  notion:
    auth:
      client_id: "{{secret.NOTION_CLIENT_ID}}"
      client_secret: "{{secret.NOTION_CLIENT_SECRET}}"
```

Well-known providers (`google`, `github`, `slack`, `microsoft`) have pre-configured
authorize and token URLs. For custom providers, specify `authorize_url` and `token_url`.

### How It Works

When an agent calls a tool on an OAuth-protected server:

1. `MCPModule._ensure_oauth_token()` resolves the user from `ExecutionContext`
2. Checks `UserStore.get_token(user_id, provider)` for a cached token
3. If token expires within 5 minutes, auto-refreshes via `refresh_token`
4. If no token exists, returns `ActionResult` with `requires_oauth: true` and `auth_url`
5. Valid tokens are injected into the transport's `Authorization` header

### Token Storage

Tokens are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before storage in the
`user_oauth_tokens` table. The encryption key is auto-generated at first launch
(`~/.digitorn/server.key`).

### API Endpoints

- `GET /api/apps/{app_id}/oauth/authorize?provider=google&server_id=gcal&session_id=...`
  Builds and returns the authorization URL.

- `GET /api/apps/{app_id}/oauth/callback?code=...&state=...`
  Exchanges the authorization code for tokens and stores them.

### PKCE Support

Google and Microsoft use PKCE (S256). The module auto-generates `code_verifier` and
`code_challenge` when the provider supports it. No configuration needed.

## Credential Auto-Discovery (Probe System)

At install time, Digitorn probes the server's installed source code to discover
the actual environment variable names it reads. This corrects mismatches between
registry metadata and actual server code.

### How probing works

1. After `npm install` or `pip install`, the probe scans `dist/`, `build/`,
   `src/`, `lib/` directories for patterns like:
   - JavaScript: `process.env.XXX`, `process.env["XXX"]`
   - Python: `os.environ.get("XXX")`, `os.environ["XXX"]`, `os.getenv("XXX")`
2. Discovered env vars are stored in the server's config
3. At connect time, a safety-net remap (`_remap_env_from_source`) ensures
   user-provided values match the actual var names the server expects

### Why this matters

The remote registry declares env var names that may differ from what the server
actually reads. For example, a server might declare `YOUR_API_KEY` in its registry
metadata but actually read `TODOIST_API_TOKEN` in its source code. The probe
catches and corrects these mismatches automatically.

## Built-in Catalog

| Server | Runtime | Package | Shorthand Keys |
|--------|---------|---------|----------------|
| GitHub | npm | `@modelcontextprotocol/server-github` | `token` |
| Notion | pip | `mcp-notion` | `token` (+ OAuth) |
| Slack | npm | `@modelcontextprotocol/server-slack` | `bot_token`, `team_id` |
| Filesystem | npm | `@modelcontextprotocol/server-filesystem` | `path` |
| Memory | npm | `@modelcontextprotocol/server-memory` | — |
| Brave Search | npm | `@modelcontextprotocol/server-brave-search` | `api_key` |
| Google Drive | npm | `@modelcontextprotocol/server-gdrive` | OAuth |
| Google Calendar | npm | `@anthropic/mcp-server-google-calendar` | OAuth |
| Linear | npm | `mcp-linear` | `api_key` |
| PostgreSQL | npm | `@modelcontextprotocol/server-postgres` | `connection_string` |
| SQLite | npm | `@modelcontextprotocol/server-sqlite` | `database` |
| Puppeteer | npm | `@modelcontextprotocol/server-puppeteer` | — |
| Todoist | pip | `todoist-mcp` | `api_key` |
| Taskboard | local | (custom) | `db_path` |

The full catalog includes ~30 servers across categories: productivity, Google
Suite, e-commerce, search/web, databases, local tools, cloud/deployment.

For servers not in the catalog, the remote registry at
`registry.modelcontextprotocol.io` provides ~800 additional servers.

## Smithery Integration

Servers hosted on [Smithery](https://smithery.ai/) can be accessed via
`via: smithery` in config:

```yaml
servers:
  github:
    via: smithery
    smithery_key: "{{secret.SMITHERY_API_KEY}}"
    smithery_namespace: my-team    # optional
```

Two modes are supported:

- **Smithery Connect** (recommended) — direct API access via
  `api.smithery.ai/connect/{namespace}/{id}/mcp`
- **Smithery Proxy** (legacy) — via `server.smithery.ai/{slug}`

The catalog maps known server IDs to Smithery slugs automatically.

## Schema Probing

At connection time, the module can probe MCP servers to discover the actual
JSON structures they expect. This is critical for LLMs without built-in API
knowledge (Qwen3, Llama, etc.).

The probe:

1. Detects "writer" tools (those accepting JSON string parameters)
2. Calls "reader" tools (search, list) to find real resource IDs
3. Calls "getter" tools (get_page, get_block) with those IDs
4. Extracts templates (strips metadata, truncates to 2500 chars per template)

Templates are injected into the agent's system prompt so the LLM can generate
correct API calls without prior knowledge.

## SDK Fix Wrapper

`sdk_fix_wrapper.py` patches a bug in the MCP Python SDK where `Optional[str]`
parameters are incorrectly converted from JSON strings to dicts. Python MCP
servers (e.g., `mcp-notion`) are automatically launched through this wrapper.

## Daemon Pool Sharing

In daemon mode (`digitorn start`), MCP connections are **shared** across apps
using the same server. The daemon pool maintains live connections with reference
counting — when all apps disconnect from a server, the connection is closed.

Use `digitorn mcp pool` to view live connections and their reference counts.
