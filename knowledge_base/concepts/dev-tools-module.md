---
id: dev-tools-module
title: "dev_tools Module - test + build Digitorn apps from inside an agent"
type: concept
keywords: [dev_tools, app, chat, run, deploy, test, compile, validate, draft, builder, pipeline, discovery]
related: [app-lifecycle, agent-spawn, common-errors, app-structure]
source: packages/digitorn/modules/dev_tools/
---

# dev_tools Module

A meta-module for agents that **build, deploy, and test other Digitorn apps** against the running daemon - this is the bridge used by the digitorn-builder pipeline (architect / compiler / tester).

The whole module exposes **3 tools, many modes** (same design as `Agent` and `Bash`): few tool names to keep the LLM schema short, dispatch via hidden parameters. Each tool wraps ~15-30 sub-operations you'd otherwise call through HTTP.

## The 3 tools

### `App` - lifecycle, discovery, drafts, secrets, MCP

One tool for everything that would be a `POST /api/apps/*` in the HTTP API. Dispatch is decided by which hidden flag you set.

Primary modes (most common in builder pipelines):

| Mode | Params to set | Returns |
|---|---|---|
| **Deploy from file** | `yaml_path=<path>` | deploy job id |
| **Deploy inline** | `yaml_content=<string>` | deploy job id |
| **Validate only** | `yaml_path=..., validate_only=true` | errors list |
| **Compile** | `yaml_content=..., compile_yaml=true` | resolved config + errors |
| **Preview prompt** | `yaml_path=..., prompt_preview=true, agent_id=<id>` | assembled system prompt |
| **Inspect app** | `app_id=<id>` | deploy metadata, agents, tools |
| **Undeploy** | `app_id=<id>, undeploy=true` | ok/fail |
| **List apps** | `list_apps=true` | deployed app_ids |
| **List modules** | `list_modules=true` | module catalogue (ground truth) |
| **List triggers** | `list_triggers=true` | trigger type catalogue |
| **Set secret** | `app_id=..., secret_key=..., secret_value=...` | ack |
| **Health** | `health=true` | daemon status |

Secondary modes: drafts (`create_draft_yaml` / `list_drafts` / `deploy_draft_id`), packages (`package_source` / `list_packages`), MCP servers (`mcp_catalog` / `mcp_install`), user credentials (`credential_provider` / `list_credentials`), tool search (`search_tools` / `get_tool`), security profile (`security_profile=true`).

### `Chat` - live conversation with a deployed app

`Chat(app_id=..., message=..., watch=true, timeout=60)` opens a session, posts the message, and **waits for `message_done`** in a single call. Use `watch=true` always - without it the sync path freezes the event loop under the tester.

Covers: sessions lifecycle, message queue, approval auto-handling, memory inspection, workspace inspection, live event tail.

### `Run` - fire-and-forget run targets

For `one_shot` apps, `pipeline` apps, trigger simulation, and background session management. Use when Chat isn't the right shape - e.g. sending a webhook payload to trigger a background session, or running a one-shot pipeline with an input.

## Typical builder-pipeline usage

```
# Compiler specialist: "make this YAML compile"
App(yaml_content=<candidate>, compile_yaml=true)
  → errors → fix → recompile → COMPILED_OK

# Tester specialist: deploy + smoke-test
App(yaml_path="app.yaml")
App(app_id="my-app")                  # confirm deployed_at fresh
Chat(app_id="my-app", message="hello", watch=true, timeout=60)
  → TEST_OK
```

## How to grant it

```yaml
capabilities:
  grant:
    - module: dev_tools
      actions: [app, chat, run]
```

Per-agent, if using specialists:

```yaml
agents:
  - id: tester
    modules:
      - {dev_tools: [app, chat, run]}
```

## Why it exists

The daemon has a rich HTTP API (`/api/apps/deploy`, `/api/apps/{id}`, `/api/sessions/.../messages`, etc.) but making an agent call them over HTTP means:
- Auth dance (bearer token per request)
- Hand-rolled loopback URL construction
- No structured error classification

`dev_tools` gives the agent **in-process** equivalents that inherit the current user's auth context and return shaped `ActionResult`s. Use this, not `http.get("http://localhost:8000/api/apps/...")`, whenever you're building tooling that runs inside a Digitorn app.

## See also

- agent-spawn - how the builder pipeline dispatches to specialists
- app-lifecycle - deploy / validate / undeploy semantics
- common-errors - compile/deploy error patterns
