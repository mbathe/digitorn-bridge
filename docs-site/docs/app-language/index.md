---

id: index
title: LLMOS App Language Reference
sidebar_position: 0
format: md
---


# LLMOS App Language Reference

The LLMOS App Language is a declarative YAML-based language for building AI applications. Define agents, tools, memory, flows, triggers, and security - all without writing a single line of code.

> **Two Execution Modes**: The App Language is the **Agentique Mode** - the LLM decides what to do autonomously. LLMOS Bridge also provides a **Compiler Mode** via the [IML Protocol](../protocol/iml-protocol.md), where you define the exact execution plan as structured JSON. Both modes share the same 18+ modules (284 actions), security pipeline, event bus, and identity system. Use YAML Apps for autonomous agents; use IML for deterministic pipelines. See the [Architecture Overview](../overview/architecture.md#two-execution-modes) for a detailed comparison.

## Documentation

| # | Guide | Description |
|---|-------|-------------|
| 1 | [Getting Started](getting-started) | Installation, first app, running |
| 2 | [App Configuration](app-config) | `app:` block, variables, metadata, interface, `module_config` |
| 3 | [Agents](agents) | Agent definition, brain, system prompt, loop |
| 4 | [Tools](tools) | Module tools, builtins, constraints |
| 4b | [Built-in Tools](builtin-tools) | `delegate`, `todo`, `memory`, `ask_user`, `emit`, `send_message` - full reference |
| 5 | [Memory](memory) | Working, conversation, episodic, project, procedural |
| 6 | [Context Management](context-management) | Token budget, compression, on-demand fetch |
| 7 | [Flows](flows) | Explicit flows, 18 step types, branching, loops, parallel |
| 8 | [Macros](macros) | Reusable flow snippets with parameters |
| 9 | [Triggers](triggers) | CLI, HTTP, schedule, webhook, watch, event |
| 10 | [Expressions](expressions) | Template syntax `{{}}`, filters, operators |
| 11 | [Security](security) | Profiles, sandbox, capabilities, approvals, audit |
| 12 | [Multi-Agent](multi-agent) | Multi-agent orchestration, strategies, communication |
| 13 | [Observability](observability) | Streaming, logging, tracing, metrics |
| 14 | [API Integration](api-integration) | Daemon API, app store, running via REST |
| 15 | [Examples](examples) | Complete real-world application examples |

## Quick Example

```yaml
app:
  name: my-assistant
  version: "1.0"
  description: "A simple AI assistant"

agent:
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
  system_prompt: |
    You are a helpful coding assistant.
    Workspace: {{workspace}}
  tools:
    - module: filesystem
      action: read_file
    - module: filesystem
      action: write_file
    - module: os_exec
      action: run_command

variables:
  workspace: "{{env.PWD}}"

triggers:
  - type: cli
    mode: conversation
    greeting: "Hello! How can I help?"

security:
  profile: power_user
```

Run it:

```bash
llmos app run my-assistant.app.yaml
```

## Architecture Overview

```
.app.yaml file
     |
     v
 AppCompiler          Parse YAML, validate schema, check semantics
     |
     v
 AppDefinition        Pydantic model tree (typed, validated)
     |
     v
 Register             Store in AppStore + link Application identity (RBAC)
     |
     v
 Prepare              Pre-load modules, warm LLM pool, health-check memory
     |
     v
 AppRuntime           Wire agent, tools, memory, triggers
     |
     +--> AgentRuntime     LLM loop (reactive/single_shot/continuous)
     |       +--> ActionSessionCache  Intra-session cache (dedup read calls, write-invalidation)
     +--> FlowExecutor     Explicit flow engine (18 step types)
     +--> MemoryManager    Multi-level memory (working/conversation/episodic/project)
     +--> ToolRegistry     Module actions + builtins resolved to LLM tool schemas
     +--> TriggerManager   CLI/HTTP/schedule/webhook/watch/event
     |
     In daemon mode:
     +--> DaemonToolExecutor   Routes through 15-step security pipeline
     +--> Application Identity Auto-created RBAC entity with linked security
     +--> SSE Streaming        Real-time events to CLI/dashboard
```

## File Convention

App files use the `.app.yaml` extension:

```
my-app.app.yaml
code-reviewer.app.yaml
research-agent.app.yaml
```
