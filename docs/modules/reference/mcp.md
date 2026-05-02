---
id: mcp
title: mcp Module
sidebar_label: mcp
sidebar_position: 13
description: Model Context Protocol — connect to stdio / SSE / HTTP MCP servers, expose their tools as native tools.
---

# mcp

The Model Context Protocol module connects to external MCP
servers and exposes their tools to the agent as if they were
native Digitorn tools. Three transports (stdio subprocess,
SSE, HTTP), per-server sandbox permissions, OAuth flows, and
auto-indexing into the agent's tool catalogue.

| Property | Value | Source |
|----------|-------|--------|
| Module id | `mcp` | `module.py:122` |
| Version | `1.0.0` | `module.py:123` |
| Type | user | |
| Pip deps | `mcp` (Anthropic SDK), `aiohttp` (for HTTP / SSE) | |

## Configuration

```yaml
tools:
  modules:
    mcp:
      config:
        servers:
          github:
            transport: stdio                  # stdio | sse | http
            command: npx                       # for stdio
            args: ["-y", "@anthropic/mcp-server-github"]
            env:
              GITHUB_TOKEN: "{{secret.GITHUB_PAT}}"
            sandbox:
              permissions: [process.exec, net.http]
              allowed_hosts: [api.github.com]

          notion:
            transport: stdio
            command: mcp-notion
            auth:
              type: oauth2
              provider: notion
              client_id: "{{secret.NOTION_CLIENT_ID}}"
              client_secret: "{{secret.NOTION_CLIENT_SECRET}}"
              env_token_var: NOTION_API_KEY
              redirect_uri: http://localhost:8913/callback
            sandbox:
              permissions: [process.exec, net.http]
              allowed_hosts: [api.notion.com]

          remote_search:
            transport: sse
            url: https://mcp.example.com/sse
            auth:
              type: oauth2
              provider: google
              client_id: "{{secret.GOOGLE_CLIENT_ID}}"
              client_secret: "{{secret.GOOGLE_CLIENT_SECRET}}"
              scopes: [https://www.googleapis.com/auth/calendar.readonly]
            sandbox:
              permissions: [net.http]
              allowed_hosts: [mcp.example.com]
        cache:
          ttl: 300
          max_size: 200
        cacheable_tools:
          github: [list_repos, get_repo, get_file_contents]
```

## The 11 actions

`module.py`. Server lifecycle + tool / resource / prompt
discovery + invocation.

| Tool | Purpose |
|------|---------|
| `mcp.connect` | Connect to a server (stdio / SSE / HTTP). |
| `mcp.disconnect` | Disconnect a server. |
| `mcp.reconnect` | Reconnect a failed server (also invoked by the auto-reconnect loop). |
| `mcp.list_servers` | List all connected servers + status. |
| `mcp.list_tools` | List tools exposed by a server. |
| `mcp.call_tool` | Invoke a tool on a specific server. |
| `mcp.list_resources` | List resources from a server. |
| `mcp.read_resource` | Read a resource from a server. |
| `mcp.list_prompts` | List prompt templates from a server. |
| `mcp.get_prompt` | Get a prompt template with arguments filled in. |
| `mcp.health` | Health check one or all servers. |

> **Auto-indexing**: every connected server's tools also
> appear in the agent's tool catalogue under the
> `mcp_<server_id>` namespace (e.g.
> `mcp_github.create_issue`). The agent calls them like any
> native tool — no need to invoke `mcp.call_tool` manually.

## Per-server sandbox

Every server **must** declare `sandbox.permissions`. A server
without a `sandbox:` block has zero OS-level rights and the
compiler refuses to ship it.

Permission categories (`sandbox/builder.py:271-309`):

| Permission | Grants |
|------------|--------|
| `process.exec`, `process.spawn_daemon`, `process.*` | seccomp `execve` / `fork`. Required for stdio transport. |
| `net.http`, `net.socket`, `net.listen`, `net.*` | seccomp `socket` / `connect`. Required for SSE / HTTP. Merges `allowed_hosts` into iptables OUTPUT rules. |
| `fs.read`, `fs.list`, `fs.*` | Add `paths.read[*]` to Landlock readable paths. |
| `fs.write`, `fs.delete`, `fs.*` | Add `paths.write[*]` to Landlock writable paths. |

Transport-aware compile warnings
(`sandbox/builder.py:199-209`):

- `stdio` without `process.exec` (or `process.*`) → warning.
- `sse` / `http` without `net.http` (or `net.*`) → warning.

## OAuth flows

`auth.type: oauth2` triggers the OAuth flow. Two transport
patterns:

| Transport | Token injection |
|-----------|-----------------|
| `sse` / `http` | `Authorization: Bearer <token>` header on every request. |
| `stdio` | Token written to the env var named in `auth.env_token_var`; subprocess **restarted** when the token refreshes. |

Both paths share the OAuth refresh loop (every 5 min, renews
within 10 min of expiry) — see
[credentials.md](../../credentials.md#oauth-flow).

5 builtin OAuth providers
(`core/credentials/oauth_providers.py`): Notion, Google,
GitHub, Slack, Discord.

`requires_oauth` flow: when the user hasn't yet authorised
that provider, the tool result carries `auth_url` for the
agent to surface — typically with a "Click here to
authorise" message.

## Smart cache

`cache.ttl` (default 300 s) and `cache.max_size` (default
200) configure an LRU. Only tools listed in
`cacheable_tools[server_id]` are cached — typically static
metadata (`list_repos`, `get_repo`) where the LLM doesn't
need fresh data. Live data (issues, PRs, emails) should be
left uncached.

## Auto-reconnect + circuit breaker

When a server's transport drops (broken pipe, socket reset,
HTTP 5xx pattern), the module reconnects with exponential
backoff. Repeated failures trip a per-server circuit breaker
that blocks calls until manually `mcp.reconnect`-ed.

## Constraints

`module.py:126`. Restricts which servers / actions are
callable.

```yaml
tools:
  modules:
    mcp:
      constraints:
        allowed_servers: [github, notion]
        max_concurrent_calls: 10
      config:
        servers: { ... }
```

## Cross-references

- App-config block reference (`tools.modules.mcp`):
  [App Configuration → tools.modules](../../app-language/02-app-config.md#toolsmodules--module-config)
- OS-level sandbox + per-server permissions:
  [OS-Level Sandbox → MCP servers](../../app-language/35-sandbox.md#mcp-servers--deny-by-default)
- Credentials vault, OAuth providers, refresh loop:
  [credentials.md](../../credentials.md)
- App-level OAuth + token injection routes:
  [API Integration → OAuth (per-app, MCP)](../../app-language/14-api-integration.md#oauth-per-app-mcp)
- MCP examples in app context:
  [Examples](../../app-language/15-examples.md) (12, 13, 14)
