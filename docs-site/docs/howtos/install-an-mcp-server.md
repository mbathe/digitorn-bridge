---
id: install-an-mcp-server
title: How to install an MCP server
---

# How to install an MCP server

Digitorn treats MCP servers like apps in an app store: the Hub
curates a catalog of well-known servers, you click **Install**,
fill any personal credential the server needs, and your agents can
reference it from any `app.yaml` by short name. Power users can
also declare arbitrary servers inline in YAML.

This page covers both paths end-to-end.

## Path 1 - Install from the Hub Catalog

This is the right path for **95% of cases**. It's the "App Store"
experience: pre-configured by Digitorn, only personal credentials
asked.

### From the dashboard

1. Open the Digitorn desktop dashboard → **Admin** → **MCP Servers**.
2. Stay on the **Catalog** tab. The list of supported servers is
   the curated catalog served by the Hub. Each card shows a short
   description, transport, and a per-server icon.
3. Click **Install** on the card you want.
4. The install dialog opens with three flavours depending on the
   server:
   - **No auth needed** (`filesystem`, `fetch`, `memory`, `time`,
     `sequential_thinking`, `git`, `everything`) - just click
     **Install server**.
   - **Personal token** (`github`, `notion`, `linear`, `clickup`,
     `stripe`, `vercel`, `cloudflare`, `apify`, ...) - paste your
     token in the single field, click **Install**. The dialog
     points to the exact provider settings page where the token
     can be generated.
   - **OAuth** (`gmail`, `google_drive`, `google_calendar`, `slack`,
     `notion`) - click **Connect with X**, finish the browser
     round-trip, you're done.
5. After install the dialog probes the server, populates its tool
   list in the **Installed** tab, and the server is now visible
   under `GET /api/mcp/available` (see below).

### From the CLI

Same result, no UI:

```bash
# List what's in the catalog
digitorn mcp catalog

# Install a no-auth server
digitorn mcp install filesystem

# Install with a token (interactive prompt for sensitive values)
digitorn mcp install github

# One-shot install with config
digitorn mcp install notion --config token=secret_xxxxxxxxxx

# Verify it's installed and listing tools
digitorn mcp test github
```

## Path 2 - Inline custom server in `app.yaml`

For servers that aren't (yet) in the Hub catalog or that have very
specific needs (private internal server, forked package, custom
flags), declare the full config under `modules.mcp.config.servers`
in your `app.yaml`.

```yaml
tools:
  modules:
    mcp:
      config:
        servers:
          my-internal-server:
            transport: stdio                  # stdio | sse | streamable_http
            command: npx
            args: ["-y", "@my-org/private-mcp"]
            env:
              MYORG_API_KEY: "{{secret.MYORG_KEY}}"
            sandbox:
              permissions: [process.exec, net.http]
              allowed_hosts: [api.my-org.com]
```

The daemon installs the npm/pip package, registers the server,
and connects on first agent turn. The same `sandbox` /
`rate_limit` / `middleware` knobs as Catalog servers apply.

## Referencing installed servers from `app.yaml`

Once a server is installed via Path 1, **you don't repeat its
configuration** in your `app.yaml`. Use the short name:

```yaml
tools:
  modules:
    mcp:
      config:
        servers:
          - github                # reference daemon-managed install
          - notion
          - filesystem
```

Or as a dict (lets you tack on per-server overrides):

```yaml
tools:
  modules:
    mcp:
      config:
        servers:
          github: {}              # reference, no overrides
          notion:
            rate_limit_rpm: 30    # only override the rate limit
          my-internal-server:     # inline custom (Path 2 above)
            transport: stdio
            command: ...
            sandbox:              # REQUIRED for inline custom servers
              permissions: [process.exec, net.http]
```

:::tip Sandbox defaults
Bare references (`- github`) and empty-dict references
(`github: {}`) auto-get the standard sandbox
`permissions: [process.exec, net.http]` so the runtime
doesn't reject the call at dispatch time. **Inline custom
servers** (with `transport:` / `command:` / `url:`) must
declare their `sandbox:` block explicitly - the compiler
refuses the deploy otherwise.
:::

The daemon resolves a bare reference in three steps:

1. **Live pool** - already connected? Share the connection.
2. **Managed store** - `managed_mcp_servers` table (the install
   from Path 1, with your saved credentials).
3. **Built-in catalog** - defaults from the baked-in catalog
   (no credentials).

If none match, the module logs an actionable error pointing at
this page. The agent still starts; the missing server's tools
simply aren't exposed.

## Discovering what's referenceable

```bash
# Daemon endpoint - returns every server_id ready to reference
curl -s https://your-daemon/api/mcp/available | jq

# Or in the dashboard: Admin → MCP Servers → "Installed" tab.
```

## Reasoning models - bump `max_tokens`

If your agent uses an OpenAI reasoning model (`gpt-5*`,
`o1`, `o3`), keep `max_tokens` ≥ `4096`. These models burn
part of the budget on internal reasoning **before** producing
visible output or tool calls; with the default `1024` you'll
often see empty assistant turns and no tool invocation.

```yaml
agents:
  - id: main
    brain:
      provider: openai
      model: gpt-5-mini
      max_tokens: 4096     # don't go below this for MCP-heavy apps
```

Non-reasoning models (Claude, GPT-4o, DeepSeek v3, Llama)
are unaffected - `1024` is fine.

## When the install fails

Common cases and their fixes:

| Symptom | Root cause | Fix |
|---|---|---|
| `'uvx' is not on PATH` | `uv` not installed on the host | macOS: `brew install uv` ⋅ Linux: `curl -LsSf https://astral.sh/uv/install.sh \| sh` ⋅ Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| `'npx' is not on PATH` | Node.js missing on the host | Install Node.js from [nodejs.org](https://nodejs.org/) or your package manager |
| `npm install failed: 404` | Upstream package was removed or renamed | Pick a different catalog entry or use Path 2 with the correct package name |
| `HTTP 401 Unauthorized` | Server requires auth not yet provided | Edit the server: dashboard → Installed → card → Configure |
| `HTTP 404 ... (SDK reports "Session terminated")` | The MCP endpoint URL is incorrect or has been retired | Verify the URL with the publisher; remove + reinstall with the correct address |
| `MCP error -32603: Invalid response format` | Transport mismatch - server speaks legacy HTTP+SSE but client opened `streamable_http` (or vice versa) | Switch `transport:` to the form the server actually speaks (`sse` ↔ `streamable_http`) |
| `MCP server '<id>' has no sandbox permissions declared` | Inline custom server in YAML without an explicit `sandbox:` block | Add `sandbox: {permissions: [process.exec, net.http]}` to the server config (bare references auto-inject the default) |
| `Server X is already installed` | A previous install succeeded but the row is still there | Uninstall via the card then reinstall, or use the existing row as-is |

For the full troubleshooting reference, see the
[mcp module reference](../reference/modules/mcp.md).
