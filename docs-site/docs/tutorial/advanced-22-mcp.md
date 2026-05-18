---
id: advanced-22-mcp
title: "Advanced 22 - MCP tool integration (Sequential Thinking)"
sidebar_label: "Advanced 22: MCP server"
---

The `mcp` module connects an app to any Model Context Protocol
server: official Anthropic servers (Sequential Thinking, Memory,
Filesystem, Fetch), community catalog entries (GitHub, Notion,
Slack), or anything published to the public MCP registry. This
tutorial wires Anthropic's `sequential-thinking` server (no
auth, no secrets) and demonstrates the catalog shorthand plus
the deny-by-default sandbox.

## What gets wired up

| Layer | Component |
|---|---|
| Server install | `npx -y @modelcontextprotocol/server-sequential-thinking` (catalog entry resolves the shorthand) |
| Transport | `stdio` (subprocess + JSON-RPC over stdin/stdout) |
| Sandbox | `process.exec` + `net.http` (default for list-form references) |
| Tool exposed to the LLM | `McpSequentialThinkingSequentialthinking` (PascalCase of `mcp.sequential_thinking.sequentialthinking`) |

The catalog ships ~30 server entries covering the popular ones;
for anything not in the catalog, spell out `command`/`args`/
`env`/`url` directly.

## The YAML

```yaml
app:
  app_id: tuto-mcp
  name: Tuto - MCP Tool Integration
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: none
  max_turns: 12
  timeout: 180
  tool_injection: direct
  direct_modules: [mcp]

agents:
  - id: main
    role: assistant
    brain:
      provider: openai
      backend: openai_compat
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      temperature: 0.2
      max_tokens: 4096
    system_prompt: |
      You are a reasoning assistant. For any non-trivial
      question, use the MCP `sequentialthinking` tool to break
      the problem into ordered thoughts. Each call records ONE
      thought with its number, total thought count, and a
      next_thought_needed boolean. Keep calling until
      next_thought_needed is false, then write the final answer.

tools:
  modules:
    mcp:
      config:
        servers:
          # Shorthand reference to the catalog entry for
          # sequential_thinking (no auth, just
          # `npx -y @modelcontextprotocol/server-sequential-thinking`).
          # The empty {} triggers the default sandbox with
          # process.exec + net.http permissions so the stdio
          # subprocess can actually start.
          sequential_thinking: {}
  capabilities:
    default_policy: auto
    max_risk_level: medium
    grant:
      - module: mcp
        actions: [call_tool, list_tools, list_servers]
```

Three things to know:

- **The shorthand `sequential_thinking: {}`** resolves through
  the daemon catalog to the full subprocess config + default
  sandbox. Anything not in the catalog needs an explicit
  block:

  ```yaml
  my_server:
    transport: stdio
    command: npx
    args: ["-y", "@scope/my-mcp-server"]
    env:
      MY_API_KEY: "{{secret.MY_KEY}}"
    sandbox:
      permissions: [process.exec, net.http]
  ```

- **Sandbox is deny-by-default.** Without a `sandbox:` block
  AND without using the catalog shorthand, every MCP tool
  call is rejected at dispatch time with
  `mcp_sandbox_blocked`. The empty `{}` value tells the
  config validator to inject the standard
  `{permissions: [process.exec, net.http]}` so the subprocess
  transport works.
- **`call_tool` is the action the agent uses.** The MCP
  module exposes more (`connect`, `disconnect`,
  `list_resources`, etc.), but only `call_tool`,
  `list_tools`, and `list_servers` need to reach the LLM.
  Granting `connect` to agents is dangerous: they could spawn
  arbitrary subprocesses.

## Deploy and run

```bash
digitorn dev deploy tuto-mcp.yaml
digitorn dev chat tuto-mcp -m "A train leaves Paris at 14:00 going 200 km/h toward Marseille. Another train leaves Marseille at 14:30 going 240 km/h toward Paris. The cities are 800 km apart. Use the sequentialthinking MCP tool to break this into 4-5 ordered thoughts. Then give the final answer: at what time do they meet, and how far from Paris?"
```

## Sample flow

The agent emits one MCP tool call per thought. Each call
returns the recorded thought back to the agent so it can
build on the previous step.

**Thought 1**:

```json
params:
{
  "thought": "Define start times and initial head start. Train A leaves Paris at 14:00 at 200 km/h. Train B leaves Marseille at 14:30 at 240 km/h. ...",
  "thoughtNumber": 1,
  "totalThoughts": 4,
  "nextThoughtNeeded": true
}
```

**Thought 2**:

```json
{
  "thought": "When Train B departs (14:30), both trains move toward each other. Relative speed = 200 + 240 = 440 km/h. Time to close remaining 700 km = ...",
  "thoughtNumber": 2,
  "totalThoughts": 4,
  "nextThoughtNeeded": true
}
```

**Thought 3**:

```json
{
  "thought": "Convert 1.590909... h to minutes: 0.590909... * 60 = 35.454545... minutes ≈ 35 minutes 27.27 seconds. So meeting time after 14:30 is ...",
  "thoughtNumber": 3,
  "totalThoughts": 4,
  "nextThoughtNeeded": true
}
```

**Thought 4**:

```json
{
  "thought": "Distance traveled by Train A = speed * time since 14:00 = 200 km/h * 2.090909... h ...",
  "thoughtNumber": 4,
  "totalThoughts": 4,
  "nextThoughtNeeded": false
}
```

**Final answer**:

```text
Plan: I'll record 4 ordered thoughts with the
sequentialthinking MCP tool (already done). Now I present
the final answer based on those steps.

Final answer:
- They meet at approximately 16:05:27 (about 16:05 and 27 seconds).
- The meeting point is about 418.18 km from Paris (≈ 418.2 km).
```

Math check: at 14:30 Train A has covered 100 km, so 700 km
remain. Closing at 440 km/h: 700 / 440 = 1.59091 h = 95 min 27 s.
Meeting at 14:30 + 1:35:27 = 16:05:27. Distance from Paris:
100 + 200 × 1.59091 = 418.18 km.

## Other catalog entries you can swap in

Just replace `sequential_thinking` with one of these:

| Catalog id | Server | Auth |
|---|---|---|
| `fetch` | Anthropic Fetch (HTTP GET) | none |
| `puppeteer` | Anthropic Puppeteer (browser automation) | none |
| `memory` | Anthropic Memory (KV store) | none |
| `filesystem` | Anthropic Filesystem (path-scoped) | none |
| `github` | GitHub repos / issues / PRs | `GITHUB_TOKEN` env |
| `notion` | Notion pages / databases | `NOTION_API_KEY` env |
| `slack` | Slack messages / channels | `SLACK_BOT_TOKEN` env |
| `postgres` | PostgreSQL read-only queries | `POSTGRES_CONNECTION_STRING` env |

For servers that need credentials, declare them in the YAML
with `env:`:

```yaml
servers:
  github:
    env:
      GITHUB_TOKEN: "{{secret.GITHUB_TOKEN}}"
```

## When to reach for MCP

- A capability already lives in an MCP server (browser
  automation, Slack, GitHub, internal company tools) and you
  do not want to re-implement it as a Digitorn module.
- You want to share tools across apps with zero per-app
  install cost: the daemon's MCP pool keeps one stdio
  subprocess per server, ref-counted across apps.
- You need the LLM to call a tool that's already well-defined
  by an external spec (MCP enforces JSON-RPC schema, so the
  tool's params are typed and validated upstream).

For purely in-process actions (filesystem, shell, workspace,
RAG), a native Digitorn module is faster and avoids the
subprocess overhead. MCP is the bridge to "everything outside
the Digitorn codebase that speaks the protocol".
