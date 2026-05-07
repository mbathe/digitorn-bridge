---
id: 04d-mcp
---

# MCP - Model Context Protocol

The `mcp` module
 connects external MCP servers to
Digitorn agents. Tools, resources, and prompts exposed by those
servers are auto-indexed and become callable like any native module
action.

Every action and field on this page maps to a real implementation
in the codebase; entries are cited with file + line.

## Module surface

`mcp/module.py` `class MCPModule` - `MODULE_ID = "mcp"`. Eleven
`@action`-decorated methods.

| Action | Source | Purpose |
|--------|--------|---------|
| `connect` | `module.py` | Open a connection to a declared MCP server. |
| `disconnect` | `module.py` | Close a server connection. |
| `reconnect` | `module.py` | Force-reconnect (drop + reopen). |
| `list_servers` | `module.py` | List declared servers and their connection state. |
| `list_tools` | `module.py` | Tools exposed by a server (or all servers). |
| `call_tool` | `module.py` | Invoke a server tool by name. |
| `list_resources` | `module.py` | Resources exposed by a server. |
| `read_resource` | `module.py` | Fetch a resource's content by URI. |
| `list_prompts` | `module.py` | Prompt templates exposed by a server. |
| `get_prompt` | `module.py` | Render a prompt template with arguments. |
| `health_check` | `module.py` | Per-server connection + capability check. |

Param classes (`mcp/params.py`): `ConnectParams`, `DisconnectParams`,
`ReconnectParams`, `ListServersParams`, `ListToolsParams`,
`CallToolParams`, `ListResourcesParams`, `ReadResourceParams`,
`ListPromptsParams`, `GetPromptParams`, `HealthCheckParams`.

## How tools land in the agent's index

The runtime indexes every connected server's tools under a synthetic
**virtual module name** built from the server id, e.g. a server
declared as `notion` exposes its tools as
`mcp_notion.<tool_name>`. The agent calls them like any other
module action - no special syntax. Discovery mode also picks them
up via `search_tools` / `browse_category`.

The `mcp` module's own actions (`connect`, `list_tools`, ...) are
exposed under the `mcp.*` FQN. Agents typically don't need them -
the daemon manages the connection lifecycle automatically based on
the YAML; the actions are there for advanced apps that want explicit
control.

## CLI

 `mcp_cli` (Typer
sub-command, registered in ). Seven commands:

```bash
digitorn mcp search <query>           # Search the catalog + registry
digitorn mcp install <server>         # Install a known server (catalog or registry)
digitorn mcp list                     # List installed / configured servers
digitorn mcp test <server_id>         # Smoke-test a connection
digitorn mcp remove <server_id>       # Uninstall
digitorn mcp pool                     # Daemon connection-pool stats
digitorn mcp health                   # Health probe across all configured servers
```

## YAML configuration

MCP servers are declared under `tools.modules.mcp.config.servers`
(map keyed by server id). Two shapes are accepted: **shorthand**
(catalog-resolved) and **explicit** (full control).

### Shorthand (catalog-resolved)

For servers known to the catalog (`mcp/catalog.py`), declare a
short form with credentials and any minimal overrides - the catalog
fills in `command`, `args`, `env`, `transport`, OAuth metadata, and
any required headers.

```yaml
tools:
  modules:
    mcp:
      config:
        servers:
          github:
            token: "{{secret.GITHUB_TOKEN}}"

          slack:
            token: "{{secret.SLACK_BOT_TOKEN}}"

          notion:
            # OAuth (catalog knows the auth provider)
```

The catalog has built-in entries for popular servers (GitHub,
Slack, Notion, Google services, Linear, ...). For anything else
published to the official MCP registry
(`registry.modelcontextprotocol.io`), the catalog falls back to
runtime registry lookup so any registered server works without code
changes.

### Explicit (full control)

When the catalog doesn't have an entry, or you need to override its
defaults, declare every transport-level field directly:

```yaml
tools:
  modules:
    mcp:
      config:
        servers:
          custom_filesystem:
            transport: stdio
            command: /usr/local/bin/my-mcp-server
            args: ["--port", "auto"]
            env:
              MY_VAR: "{{env.MY_VAR}}"
            timeout: 30
            sandbox:
              permissions: [process.exec, fs.read]
              paths:
                read: ["{{workdir}}"]
                write: []

          remote_search:
            transport: streamable_http
            url: https://search.example.com/mcp
            headers:
              Authorization: "Bearer {{secret.SEARCH_API_KEY}}"
```

The presence of `command:`, `url:`, or an explicit `transport:` key
**bypasses both the catalog and the registry** - the entry is taken
verbatim. This is intentional: lets you wire up bespoke servers
without arguing with the catalog.

### Per-server fields

Common keys (recognised in both shorthand and explicit shapes;
`mcp/catalog.py:_STANDARD_KEYS`):

| Key | Description |
|-----|-------------|
| `transport` | One of `stdio`, `sse`, `streamable_http`. Required for explicit configs. |
| `command` / `args` / `env` | stdio transport - process launch. |
| `url` / `headers` | sse / streamable_http - HTTP endpoint. |
| `timeout` | Request timeout (seconds). |
| `buffer_size` | Stdio buffer size. |
| `auth` | OAuth provider id (e.g. `notion`, `google`). |
| `examples` | Optional curated examples used by the prompt builder. |
| `rate_limit_rpm` | Per-server rate limit hint. |
| `via` | Routing override (e.g. `smithery` proxy). |
| `smithery_key`, `smithery_namespace`, `smithery_slug` | Smithery hosting metadata. |
| `sandbox` | OS-level sandbox config (next section). |

## Transports

`mcp/transports.py` ships three transport classes
(`MCPTransport` Protocol at line 34):

| Transport | Class | When to use |
|-----------|-------|-------------|
| `stdio` | `StdioTransport` (`transports.py`) | Server runs as a subprocess; communication over stdin/stdout. The default for local servers (npm-installed, native binaries, Docker). |
| `sse` | `SSETransport` (`transports.py`) | Server-Sent Events over HTTP. Long-lived connection, server pushes events. |
| `streamable_http` | `StreamableHTTPTransport` (`transports.py`) | Bidirectional HTTP streaming. The newest transport, used by hosted servers (Smithery, Cloudflare Workers, ...). |

For `stdio`, the daemon spawns the server process and pipes
JSON-RPC messages over stdin/stdout. The process inherits the
sandbox declared in the config (see next section). For `sse` and
`streamable_http`, the daemon opens an outbound HTTP connection
and tunnels JSON-RPC over it.

## Per-server sandbox

`schema.py` `MCPServerSandbox` (`extra: forbid`). Every MCP
server **must** declare what it needs - no declaration = no
OS-level rights (deny by default).

```yaml
servers:
  github:
    command: npx @modelcontextprotocol/server-github
    sandbox:
      permissions: [process.exec, net.http]
      paths:
        read: ["{{workdir}}"]
        write: []
      allowed_hosts: [api.github.com]
```

Three top-level fields (`schema.py`, 648, 656`):

| Field | Type | Description |
|-------|------|-------------|
| `permissions` | list[string] | OS-level permissions the server needs. See the table below. |
| `paths` | dict[string, list[string]] | Filesystem paths beyond the workspace. Keys: `read` (read-only) and `write` (read-write). Supports `{{workdir}}` and `~` expansion. |
| `allowed_hosts` | list[string] | Allowed network hosts for outbound connections. Only effective when `net.http` or `net.socket` is granted. |

Permission categories
(`schema.py` docstring, source of truth):

| Permission | What it grants |
|------------|---------------|
| `process.exec` | Spawn subprocesses (required for the stdio transport). |
| `process.*` | All process permissions (exec + spawn_daemon). |
| `net.http` | Outbound HTTP (required for sse / streamable_http transports). |
| `net.socket` | Raw socket access. |
| `net.listen` | Bind / listen on a port. |
| `net.*` | All network permissions. |
| `fs.read` | Read files beyond the workspace. |
| `fs.write` | Write files beyond the workspace. |
| `fs.delete` | Delete files beyond the workspace. |
| `fs.*` | All filesystem permissions. |

Paths and host allowlists are evaluated by the daemon's sandbox
layer. See [OS Sandbox](35-sandbox.md) for the full kernel-level
isolation model (Landlock / seccomp / Seatbelt / Job Objects).

## Smithery - hosted servers

The catalog supports two routes through Smithery
(`mcp/catalog.py:_SMITHERY_CONNECT_BASE`,
`_SMITHERY_PROXY_BASE`):

- **Smithery Connect** (recommended) -
  `https://api.smithery.ai/connect`. Uses `streamable_http`
  transport with a Smithery-issued URL keyed by your account.
- **Smithery Proxy** (legacy) -
  `https://server.smithery.ai`. The proxy runs the server
  remotely; Digitorn talks to it like any other HTTP MCP server.

Built-in slug map at `_SMITHERY_SLUGS` (e.g. `github` →
`@smithery-ai/github`, `slack` → `@smithery-ai/slack`). When a
server is declared with `via: smithery`, the catalog rewrites the
config to point at the appropriate Smithery endpoint.

## OAuth flow

For servers that authenticate per-user (Google Calendar, GitHub
user-scope, Notion personal workspace, ...), Digitorn ships a built-in
OAuth flow keyed by the catalog provider id.

The MCP module declares the well-known providers; each entry is a
`{ authorize_url, token_url, revoke_url? }` map. Verified providers
in code today: `google`, `github`, `slack`. Other providers can be
routed through the generic OAuth2 client.

The flow:

1. Agent (or installer) calls `mcp.connect(server="<id>")`.
2. The daemon checks for a valid token in the credentials vault.
3. **Missing or expired token** - the call returns
   `{ requires_oauth: true, authorize_url, state }`. The client
   opens that URL in a browser; the user grants access.
4. The daemon's OAuth callback endpoint exchanges the code,
   stores the token in the vault under the right scope
   (`per_user` by default), and refreshes it in the
   background.
5. Next call to `mcp.connect` succeeds transparently.

PKCE (Proof Key for Code Exchange) is enabled for every public
client. Refresh tokens are stored encrypted; a daemon-side loop
refreshes them within 10 minutes of expiry.

For **stdio servers that take an OAuth token via env var** (e.g.
`GITHUB_PERSONAL_ACCESS_TOKEN`), the catalog declares
`env_token_var` so the runtime injects the freshly-issued or
refreshed token automatically - no manual restart.

For the full credentials surface (scopes, vault, audit log), see
[credentials.md](../reference/runtime/credentials.md).

## Capabilities and security

MCP tools land in the agent index alongside native tools, so the
same [capabilities](11-security.md) machinery applies:

```yaml
tools:
  modules:
    mcp:
      config:
        servers:
          github: { token: "{{secret.GITHUB_TOKEN}}" }
          notion: { auth: notion }
  capabilities:
    default_policy: auto
    grant:
      - { module: mcp_notion }                         # all notion tools auto-allowed
    approve:
      - { module: mcp_github, actions: [delete_repo] } # require user OK on destructive
    deny:
      - { module: mcp_github, actions: [merge_pr] }    # never allow
```

Without a `capabilities:` block, MCP tools are exposed but
governed by the daemon's default policy (`approve` - every call
prompts for confirmation). Production apps should declare an
explicit `tools.capabilities` to make the security posture
intentional.

## Resources and prompts

MCP servers can expose two surfaces beyond tools:

- **Resources** - addressable data (a notion page, a github file,
  a slack message). The agent calls `mcp.list_resources(server=...)`
  and `mcp.read_resource(uri=...)`. Resources are not auto-indexed;
  they must be fetched explicitly.
- **Prompts** - server-defined prompt templates. The agent calls
  `mcp.list_prompts(server=...)` to discover them and
  `mcp.get_prompt(server=..., name=..., arguments=...)` to render
  one. The rendered text comes back as the response - the agent
  then uses it as part of its own reasoning.

Both are handled by the actions listed at the top of this page.

## Lifecycle

| Stage | What happens |
|-------|--------------|
| **Deploy** | Compiler validates the `tools.modules.mcp.config.servers` block: every server resolves either via the catalog or has a complete explicit config. Sandbox declarations are checked. |
| **Activation** (per-session start) | The daemon opens a connection to each server (stdio spawns the process, HTTP transports establish the streaming socket). OAuth tokens are loaded from the vault. |
| **Per-turn** | The agent calls `mcp_<server>.<tool>(...)` or, in discovery mode, finds them via `search_tools`. Tool calls are routed through the connection pool. |
| **Undeploy / shutdown** | All connections close cleanly (stdio processes terminated, sockets closed). OAuth tokens stay in the vault for the next deploy. |

The connection pool is shared across sessions on the same daemon
(`mcp/connections.py`). Stats: `digitorn mcp pool`.

## Result normalisation and caching

Two pieces sit between the raw transport and the agent:

- **Result normaliser** (`mcp/protocol.py`) - wraps the
  transport-specific response into the `ActionResult` shape so
  agents see consistent return values regardless of which server
  generated them.
- **Smart cache** (`mcp/cache.py`) - caches read-only operations
  (`list_tools`, `list_resources`, `list_prompts`, idempotent
  `read_resource` calls) with a TTL. Tool calls themselves are
  never cached - they're assumed to have side effects unless
  explicitly marked otherwise.

## Schema probing for tool examples

When a server's `list_tools` response doesn't include example
arguments, the daemon optionally probes each tool with a typed
no-op call to discover its argument shape and stash a few
examples. Behaviour is in `mcp/schema_probe.py`. Disable per-app
via the module's config when the probing introduces unwanted
side-effects.

## Middleware

The MCP module supports the same [middleware pipeline](17-middleware.md)
shape as any other module. Add per-MCP-server middleware via
`tools.modules.mcp.middleware`, or wrap individual server config
under `tools.modules.mcp.config.servers.<id>.middleware`.

```yaml
tools:
  modules:
    mcp:
      middleware:
        - { audit: { log_params: true } }
        - { rate_limit: { rpm: 60 } }
```

## Limitations / current gotchas

- **Sandbox enforcement** - `MCPServerSandbox` is enforced by the
  OS sandbox layer the daemon runs under. Apps deployed against a
  daemon without OS-level sandboxing (Linux without Landlock /
  seccomp, or Windows without Job Objects) get advisory enforcement
  only. Match this with `runtime` constraints if you need hard
  guarantees.
- **Streamable HTTP** is the newest transport - some self-hosted
  MCP servers still only support `stdio` or `sse`. Prefer those if
  the catalog entry doesn't have a streamable variant.
- **Per-tool risk classification** - the runtime treats every
  tool exposed by an MCP server with the **same** default policy
  (since servers don't broadcast risk levels). If you need finer
  control, use `tools.capabilities.approve` and
  `tools.capabilities.deny` to single out destructive tools.

## Cross-references

- App-level configuration block: [App Configuration](02-app-config.md)
- Tool delivery and discovery: [Tools](04-tools.md)
- Capabilities (grant / approve / deny): [Security](11-security.md)
- Credentials vault, scopes, audit log:
  [credentials.md](../reference/runtime/credentials.md)
- OS-level sandbox: [OS Sandbox](35-sandbox.md)
- Per-module reference: [modules/reference/mcp.md](../reference/modules/mcp.md)
