---
id: modules-index
title: Module reference
---

# Module reference

The daemon ships 23 modules under Each module exposes a set of `@action`-decorated methods that
agents call as tools. To enable a module in an app, add it under
`tools.modules.<id>` in the YAML; the [language reference](../../language/04-tools.md)
documents the shape of that block.

`context_builder` and `llm_provider` are auto-loaded on every app
and never declared explicitly.

## By category

### Core I/O

| Module | One-liner |
|--------|-----------|
| [filesystem](filesystem.md) | Read, write, edit, glob, grep on the host filesystem. |
| [shell](shell.md) | Run shell commands. Git Bash on Windows, bash/zsh on POSIX. |
| [http](http.md) | HTTP requests. |
| [web](web.md) | Web search + fetch + content extraction. |
| [database](database.md) | Connect to SQL databases (sqlite, postgres, mysql). |

### Intelligence and orchestration

| Module | One-liner |
|--------|-----------|
| [memory](memory.md) | Cognitive memory: goals, todos, facts. Survives compaction. |
| [agent_spawn](agent_spawn.md) | Spawn sub-agents (8 modes via one `Agent` tool). |
| [behavior](behavior.md) | Runtime rule engine: pre/post-tool checks, semantic classifier. |
| [context_builder](context_builder.md) | Auto-loaded. Tool index, discovery meta-tools, prompt assembly. |
| [llm_provider](llm_provider.md) | Auto-loaded. LLM call dispatch, fallback brain, named providers. |

### Knowledge and retrieval

| Module | One-liner |
|--------|-----------|
| [rag](rag.md) | Hybrid retrieval over knowledge bases (BM25 + dense + Text2SQL). |
| [vector](vector.md) | Vector store primitive (Qdrant, Chroma, in-memory). |
| [index_module](index_module.md) | Token-aware code indexing for IDE-style search. |

### UI surfaces

| Module | One-liner |
|--------|-----------|
| [workspace](workspace.md) | In-memory virtual filesystem mirrored to the client. |
| [preview](preview.md) | Internal SSE transport. Auto-loaded by workspace. |
| [web_preview](web_preview.md) | Iframe attachment registry for spawned dev servers. |
| [widget](widget.md) | Declarative UI tree (43 primitives; 7 module actions, 15 client-side action-types). |

### Integration

| Module | One-liner |
|--------|-----------|
| [mcp](mcp.md) | Connect to external MCP servers. |
| [channels](channels.md) | Bidirectional I/O: webhook, cron, file_watcher, email, RSS, queue, slack, discord, telegram, voice. |
| [lsp](lsp.md) | Language Server Protocol diagnostics passthrough. |
| [queue](queue.md) | Message queue primitive (Redis, in-memory). |
| [cron_native](cron_native.md) | Cron scheduler (`schedule`, `cancel_schedule`, `remind`). |
| [dev_tools](dev_tools.md) | Development conveniences. |

## Tool naming

Modules expose actions via a fully-qualified name (FQN) like
`shell.bash` or `filesystem.write`. The runtime promotes a curated
subset to short PascalCase names (`Bash`, `Write`, `Edit`, `Grep`,
`Glob`, `Agent`, `Remember`, `TaskCreate`, `WsWrite`, ...) so the LLM
sees the same names a human developer sees in Claude Code, Cursor,
and other agentic IDEs. The mapping is centralised in When restricting per-agent module access ([Agents - Per-agent
module access](../../language/03-agents.md#per-agent-module-access)),
list the action names in their FQN suffix form
(`{ shell: [bash] }`, not `{ shell: [Bash] }`).

## Adding your own module

Modules are Python packages that subclass `BaseModule` and decorate
methods with `@action`. The full surface (`CONFIG_MODEL`, params,
constraints, slots, capability slots, `register_handler`) is
covered in [How to add a module](../../howtos/add-a-module.md).
