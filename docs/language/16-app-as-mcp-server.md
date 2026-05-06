---
id: app-as-mcp-server
---

# App-as-MCP-Server

> **Status: not implemented.** No code path under
> `packages/digitorn/` exports a deployed app as an MCP server
> today. This page documents the **design intent** and points to
> the working alternatives.

A deployed Digitorn app could one day expose itself as an
MCP server, letting any MCP client (Claude Desktop, Cursor,
Windsurf, another Digitorn daemon, ...) call the app's tools as
if they were native MCP tools. The app's `runtime.input` /
`runtime.output` would map to `tools/list` and `tools/call`
in the MCP protocol; capabilities, sandbox, and audit would
apply to the inbound call exactly as they do for in-process
tool calls.

The use case is the inverse of [MCP Servers](04d-mcp.md) — that
page documents how Digitorn **consumes** MCP servers; this one
covers the planned ability to **be** an MCP server.

## What works today instead

Three patterns cover most of the use cases without needing the
MCP-export feature:

### 1. `call_app` from another Digitorn daemon

If both the calling and called sides are Digitorn daemons, use
the `call_app` primitive — same daemon or via HTTP across
daemons:

```json
{"name": "call_app",
 "arguments": {"app_id": "code-analyzer",
               "input": "src/auth.py"}}
```

Works today. The target app must be deployed and in
`runtime.mode: one_shot`. See
[Composition → call_app](22-composition.md#pattern-1--call_app-in-agent-app-invocation).

### 2. Multi-agent within one app

When the caller and the "tool" can live inside one app, use
multi-agent: declare the would-be MCP server as a specialist
agent and call it via `Agent(specialist='...')`. Sub-agents
share the workspace + memory + the 5 shared modules with the
coordinator. See [Multi-Agent](12-multi-agent.md).

### 3. Plain HTTP API

A deployed app already exposes `POST
/api/apps/<app_id>/run` — that's how `call_app` works
internally
(`actions_meta.py`). External clients (Cursor, custom
scripts, ops tooling, another runtime) can post to this endpoint
directly with `{"input": "..."}` and read the response. The
auth requirements
([Auth](22-auth.md)) apply.

```bash
curl -X POST "https://api.example.com/api/apps/code-analyzer/run" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"input": "src/auth.py"}'
```

This is a JSON HTTP API, not the MCP wire protocol — but for any
non-Claude-Desktop client it's the same shape (post a payload,
read the response).

## When `app-as-mcp-server` would land

The design tracker for this feature lives in the project's
internal roadmap. This page will be replaced with a real
reference once the export path ships. Until then, treat any
`app-as-mcp-server`-related YAML field as **unsupported** — the
canonical schema's `extra: forbid` will reject anything that
doesn't match the eight blocks documented in
[App Configuration](02-app-config.md).

## Cross-references

- Calling another deployed Digitorn app today:
  [Composition → call_app](22-composition.md#pattern-1--call_app-in-agent-app-invocation)
- Calling external MCP servers from a Digitorn app:
  [MCP Servers](04d-mcp.md)
- Multi-agent inside one app (the closest one-app substitute):
  [Multi-Agent](12-multi-agent.md)
- The 8-block canonical schema:
  [App Configuration](02-app-config.md#yaml-structure)
