---
id: mcp
title: mcp Module
sidebar_label: mcp
sidebar_position: 13
description: Model Context Protocol - connect to stdio / SSE / HTTP MCP servers, expose their tools as native tools.
---

# mcp

The Model Context Protocol module connects to external MCP
servers and exposes their tools to the agent as if they were
native Digitorn tools. Three transports (stdio subprocess,
SSE, HTTP), per-server sandbox permissions, OAuth flows, and
auto-indexing into the agent's tool catalogue.

| Property | Value |
|----------|-------|
| Module id | `mcp` |
| Version | `1.0.0` |
| Type | user |
| Pip deps | `mcp` (Anthropic SDK), `aiohttp` (for HTTP / SSE) |

## The App Store model

Digitorn's MCP catalog is curated like an App Store: each entry
declares not only what the user fills, but also what **Digitorn**
provides on their behalf — shared API keys for free-tier search
backends, hosted infrastructure URLs (e.g. a public Cloudflare
Worker bridge), and the OAuth client ids registered once for the
whole user base.

Each `mcp_featured_entries` row carries three classification fields:

| Column | Semantic | Example (`brave_search`) |
|---|---|---|
| `personal_keys` | Subset of `env_mapping` keys the **user** fills personally. Shown prominently in the install dialog. | `[]` (no personal field needed) |
| `digitorn_provided` | `{env_var_name: credential_name}` map of env vars **Digitorn** injects at install time from the system-wide credential store. Hidden from the user. | `{"BRAVE_API_KEY": "brave_shared"}` |
| `hosted_url` | Optional Digitorn-managed endpoint (shared bridge / proxy). Filled into the canonical URL field when the user hasn't supplied one. | (empty for Brave Search) |

The daemon's install path consumes these at row-insert time:

1. User clicks **Install** on a catalog card.
2. Daemon fetches the catalog entry through the Hub proxy
   (`hub_catalog_client`, 5 min in-memory cache).
3. For each `digitorn_provided[env_var]` pair, the daemon calls
   `CredentialStore.get_credential_by_name(name, scope=SYSTEM_WIDE,
   decrypt=True)` and writes the resolved value into the subprocess
   env. User-supplied config still wins if it sets the same env var.
4. If `hosted_url` is set and the user hasn't supplied a URL,
   the daemon uses the hosted URL as the connection endpoint.
5. The row is persisted with the resolved env so subsequent starts
   pick up the values without re-resolving.

### Operator workflow

To turn a catalog entry into a "1-click install, zero personal
field" experience:

1. **Provision the shared credential** in the daemon's credential
   store (system_wide scope). For Brave Search:
   ```bash
   curl -X POST https://your-daemon/api/credentials \
     -H "Authorization: Bearer $ADMIN_TOKEN" \
     -d '{
       "name": "brave_shared",
       "scope": "system_wide",
       "handler": "api_key",
       "fields": {"api_key": "BSA-xxxxxxxxxxxxxxxxxx"}
     }'
   ```
2. **Tag the Hub entry** to point at it:
   ```bash
   curl -X PATCH https://hub.digitorn.ai/api/v1/mcp/featured/brave_search \
     -H "Authorization: Bearer $HUB_ADMIN_TOKEN" \
     -d '{
       "personal_keys": [],
       "digitorn_provided": {"BRAVE_API_KEY": "brave_shared"}
     }'
   ```
3. **Wait for the daemon's 5 min cache to refresh** (or hit
   `POST /api/mcp/registry/refresh` on the daemon for an immediate
   pull).
4. Users now see Brave Search install with **no fields** — just a
   single **Install** button.

If the shared credential isn't provisioned yet, the entry still
ships: the install just falls back to asking the user. There's no
hard dependency between the Hub metadata and the credential store.

### Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `digitorn_provided_credential_missing` in daemon logs | The Hub entry references a credential name that doesn't exist (or is in the wrong scope) in this daemon's credential store. | Provision the credential, OR `PATCH` the entry to remove the reference. |
| `digitorn_provided_skip … reason=no_credential_store_in_install_context` | The API route didn't thread `request.app.state.credential_store` (regressed wiring). | File an issue: this should never happen in production. |
| User fills the field anyway and it overrides the shared key | Intentional. User-supplied values win, lets a power user opt out of the shared quota. | None needed. |

## Two ways to add a server

The MCP module accepts servers from two complementary places:

1. **Hub Catalog** (default for 95% of cases). Browse the curated
   list of MCP servers in the Digitorn dashboard, click **Install**,
   fill any personal credential the server needs (your GitHub PAT,
   your Notion key, …) and the daemon registers it once for every
   app on this machine. Your `app.yaml` then references it by short
   name — see [Referencing installed servers](#referencing-installed-servers).
2. **Inline custom config in YAML** (power-user path). For servers
   that aren't (yet) in the Hub catalog or have very specific needs,
   provide the full transport / command / env block under
   `modules.mcp.config.servers.<id>` in your `app.yaml`. The daemon
   installs the package, registers the server, and connects.

Both paths converge on the same module surface — once registered,
your agent calls the server's tools through the same interface
regardless of how it got there.

## Referencing installed servers

When a server is **already installed in the daemon** (via the Hub
Catalog UI or the CLI), your `app.yaml` doesn't need to repeat any
of its configuration. Three shorthand forms are accepted:

```yaml
# Form 1 — list of names
modules:
  mcp:
    config:
      servers:
        - github
        - notion

# Form 2 — dict with empty config
modules:
  mcp:
    config:
      servers:
        github: {}
        notion: {}

# Form 3 — mix of references + inline overrides
modules:
  mcp:
    config:
      servers:
        github: {}                       # use daemon install as-is
        notion:
          token: "{{secret.MY_OTHER_NOTION_KEY}}"   # override one field
        my-internal-server:              # inline custom (see below)
          transport: stdio
          command: ./bin/my-mcp
          args: ["--mode=read-only"]
```

How the daemon resolves a bare reference at module init time:

1. **Live pool** — if the server is already connected, the app shares
   that connection (zero re-init cost).
2. **Managed store** — read the row from `managed_mcp_servers`
   (resolved package, command, env including credentials).
3. **Built-in catalog** — fall back to the catalog entry's defaults
   (no credentials — useful for no-auth servers like `filesystem`,
   `fetch`, `memory`).

If none of the three resolve, the module logs an actionable error
and skips that server. The agent still starts; the tools from the
missing server simply aren't exposed.

### Discovering what's referenceable

The daemon exposes `GET /api/mcp/available` which returns every
`server_id` currently ready in the managed store. Useful for editor
autocomplete or scripted deploys:

```bash
curl -s https://your-daemon/api/mcp/available | jq '.available[].server_id'
"github"
"notion"
"filesystem"
```

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
| `mcp.get_prompt` | Get a prompt template with arguments filled in |
| `mcp.health` | Health check one or all servers. |

> **Auto-indexing**: every connected server's tools also
> appear in the agent's tool catalogue under the
> `mcp_<server_id>` namespace (e.g.
> `mcp_github.create_issue`). The agent calls them like any
> native tool - no need to invoke `mcp.call_tool` manually.

## Per-server sandbox

Every server gets a `sandbox.permissions` set at compile time.
Three rules to know:

1. **Bare references get the standard sandbox**: writing
   `- fetch` (list form) or `fetch: {}` (dict-empty form) is
   shorthand for *"trust the daemon's curated install"* — the
   module auto-injects `permissions: [process.exec, net.http]`
   so the call isn't blocked at dispatch time with the cryptic
   ``mcp_sandbox_blocked`` error. If you need a tighter
   sandbox, spell the block out (rule #2 below).
2. **Explicit `sandbox:` always wins**: any block you write
   under a server is preserved verbatim — bare or not.
3. **Custom inline servers must declare sandbox**: when you
   provide `transport` / `command` / `url` for a server not
   in the daemon's managed catalog, the compiler requires an
   explicit `sandbox:` block. Without it the deploy fails:
   ``modules.mcp.config.servers.<id>: No 'sandbox' block declared``.

Permission categories (`sandbox/builder.py`):

| Permission | Grants |
|------------|--------|
| `process.exec`, `process.spawn_daemon`, `process.*` | seccomp `execve` / `fork`. Required for stdio transport. |
| `net.http`, `net.socket`, `net.listen`, `net.*` | seccomp `socket` / `connect`. Required for SSE / HTTP. Merges `allowed_hosts` into iptables OUTPUT rules. |
| `fs.read`, `fs.list`, `fs.*` | Add `paths.read[*]` to Landlock readable paths. |
| `fs.write`, `fs.delete`, `fs.*` | Add `paths.write[*]` to Landlock writable paths. |

Transport-aware compile warnings
(`sandbox/builder.py`):

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
within 10 min of expiry) - see
[credentials.md](../../reference/runtime/credentials.md#oauth-flow).

5 builtin OAuth providers: Notion, Google,
GitHub, Slack, Discord.

`requires_oauth` flow: when the user hasn't yet authorised
that provider, the tool result carries `auth_url` for the
agent to surface - typically with a "Click here to
authorise" message.

## Smart cache

`cache.ttl` (default 300 s) and `cache.max_size` (default
200) configure an LRU. Only tools listed in
`cacheable_tools[server_id]` are cached - typically static
metadata (`list_repos`, `get_repo`) where the LLM doesn't
need fresh data. Live data (issues, PRs, emails) should be
left uncached.

## Auto-reconnect + circuit breaker

When a server's transport drops (broken pipe, socket reset,
HTTP 5xx pattern), the module reconnects with exponential
backoff. Repeated failures trip a per-server circuit breaker
that blocks calls until manually `mcp.reconnect`-ed.

## Server config shapes (YAML reference)

Every shape under `modules.mcp.config.servers` resolves to the
same internal form `{server_id: server_config}`. Pick the form
that fits the install you're describing — they are equivalent
end results, only the syntax differs.

```yaml
modules:
  mcp:
    config:
      servers:
        # 1. Bare list — references daemon-managed installs.
        #    Default sandbox auto-injected (process.exec + net.http).
        - github
        - filesystem

        # 2. Bare dict (empty value) — same semantics as form 1.
        #    Default sandbox auto-injected.
        notion: {}

        # 3. Dict with a custom field — still treated as a reference
        #    but with an override (rate limit, middleware, sandbox).
        linear:
          rate_limit_rpm: 30

        # 4. Inline custom server — full config the user owns end-to-end.
        #    The compiler REQUIRES an explicit sandbox block here.
        my_internal:
          transport: stdio
          command: ./bin/my-mcp
          args: ["--read-only"]
          sandbox:
            permissions: [process.exec, fs.read]
```

The runtime resolves bare references through three paths in
order: live pool, `managed_mcp_servers` row, then built-in
catalog. The custom inline server bypasses all three (its
config IS the source of truth).

## Cross-OS prerequisites

The daemon installs MCP server packages into an isolated
directory per server. The exact path resolves via
`platformdirs.user_data_dir("digitorn") / "mcp-servers"`:

| OS | Install root |
|----|-------------|
| Windows | `%LOCALAPPDATA%\digitorn\digitorn\mcp-servers\` |
| macOS | `~/Library/Application Support/digitorn/mcp-servers/` |
| Linux | `~/.local/share/digitorn/mcp-servers/` |

Runtimes the daemon discovers automatically (nvm /
nvm-windows / fnm / volta / Homebrew / official installers
for Node.js; the system Python for venvs; `uv` if installed):

| Runtime tool | Required for | Install (one of) |
|---|---|---|
| `node` + `npm` / `npx` | npm-based MCP servers | [nodejs.org](https://nodejs.org/) — Linux: `apt install nodejs npm` — macOS: `brew install node` |
| `python3` | pip-based MCP servers via stdlib `venv` | usually already present |
| `uv` / `uvx` | faster pip installs **and** servers using `uvx` as their entry point | Linux: `curl -LsSf https://astral.sh/uv/install.sh \| sh` — macOS: `brew install uv` — Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |

After installing a runtime tool, **restart the daemon** so it
picks up the new `PATH`. Without restart, an MCP install that
needs the tool will fail at `connect` time with a clear
`Command not found` message (see below).

## Error catalog

The transport layer normalises every failure into a single
`MCPTransportError` with a `code` (HTTP status when one
applies) and a user-actionable message. This is what the
agent sees in the tool-result `error` field and what the
dashboard surfaces in the install / connect dialogs.

| Symptom | Code | Cause | Fix |
|---|---|---|---|
| `HTTP 401 Unauthorized at <url>` | 401 | Remote server requires auth not yet supplied | Configure the server's `auth:` block (env-token or OAuth) and reconnect |
| `HTTP 403 Forbidden at <url>` | 403 | Credentials present but lacking the required scope | Mint a new token with broader scope, re-attach |
| `HTTP 404 Not Found at <url>` | 404 | Endpoint path is wrong or has been retired | Verify the URL with the publisher; for stdio, re-install |
| `HTTP 404 ... (SDK reports "Session terminated")` | 404 | SDK relabels any 404 on the JSON-RPC POST as "Session terminated" — usually the URL is incorrect or the server moved | Same as 404 — re-check the URL |
| `MCP error -32603: Invalid response format` | server-side | Transport mismatch (server speaks legacy HTTP+SSE but the client opened `streamable_http`, or vice versa) | Switch `transport:` to the form the server actually speaks — try `sse` if you had `streamable_http` |
| `Command not found: <tool>` | -1 | A runtime (`npx`, `uvx`, `pipx`) referenced by the server config isn't on the daemon's `PATH` | Install the missing runtime per the table above, restart the daemon |
| `'uvx' is not on PATH` (specific) | -1 | The server uses `uvx` (Astral's tool runner) but `uv` isn't installed | Install `uv` (see prerequisites), restart |
| `MCP server '<id>' has no sandbox permissions declared` | -1 | Custom inline server without an explicit `sandbox:` block (bare references auto-inject the default) | Add `sandbox: {permissions: [process.exec, net.http]}` to the server config in YAML |
| `Server '<id>' is already installed (status=ready)` | 400 | Re-running install on an existing row | Uninstall first (`DELETE /api/mcp/servers/<id>`) then re-install |

## Reasoning models — token budget

Reasoning models (OpenAI `gpt-5*`, `o1`, `o3`) consume part
of their `max_tokens` budget on internal reasoning **before**
producing any visible output or tool call. With `max_tokens:
1024` the budget is often entirely spent on reasoning,
leaving the assistant turn empty (no text, no tool call).

For MCP-heavy apps using a reasoning model, raise `max_tokens`
to at least `4096`:

```yaml
agents:
  - id: main
    brain:
      provider: openai
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      max_tokens: 4096      # was 1024 — too tight for reasoning
```

Non-reasoning models (Claude Haiku/Sonnet, GPT-4o, DeepSeek
v3, Llama, ...) work fine with the default `1024`.

## Tool schema → OpenAI sanitization

OpenAI's function-calling API only accepts a restricted set
of JSON Schema `format` keywords. MCP servers often declare
broader formats (`uri`, `regex`, `relative-uri`, ...) that
trigger `OpenAIException - Invalid schema for function 'X':
In context=('properties', 'Y'), 'Z' is not a valid format`.

The provider auto-strips any `format` value not in
`{date, date-time, time, duration, email, hostname, ipv4,
ipv6, uuid}` before sending the schema to OpenAI. No
configuration needed; the sanitisation is transparent. If
you see a `400 BadRequest` mentioning a `format` value, file
an issue — that's a regression in the sanitiser, not a YAML
configuration problem.

## Live testing

End-to-end MCP scenarios live in
`tools/live_tests/mcp_e2e_scenarios.py`. They exercise all
three install paths (catalog list-form, catalog dict-form,
inline custom YAML) against the real daemon with a real LLM
chat. Run after any change touching the module:

```bash
py -3.12 tools/live_tests/mcp_e2e_scenarios.py
```

Scenarios check `tool_call.payload.success == True` —
the assertion fires only when the tool actually executed,
not when the LLM merely emitted a `tool_call` event.

## Constraints

`module.py`. Restricts which servers / actions are
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
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- OS-level sandbox + per-server permissions:
  [OS-Level Sandbox → MCP servers](../../language/35-sandbox.md#mcp-servers---deny-by-default)
- Credentials vault, OAuth providers, refresh loop:
  [credentials.md](../../reference/runtime/credentials.md)
- App-level OAuth + token injection routes:
  [API Integration → OAuth (per-app, MCP)](../../language/14-api-integration.md)
- MCP examples in app context:
  [Examples](../../language/15-examples.md) (12, 13, 14)
