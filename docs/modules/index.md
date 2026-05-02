---
id: modules-index
---

# Modules

Modules are the building blocks of agent capabilities in
Digitorn. Each module exposes a set of `@action`-decorated
methods that agents discover and execute at runtime.

A module is self-contained: it declares its own actions,
parameters, risk levels, permissions, and lifecycle hooks.
The framework handles discovery, routing, security
enforcement, and context injection automatically.

> **Mounted under `tools.modules.<id>:` in the canonical
> 8-block YAML.** Anything else under a module block
> (`config`, `setup`, `constraints`, `middleware`) is
> validated against the strict `ModuleBlock` schema —
> unknown keys are silently dropped (a recurring source of
> bugs; always nest config under `config:`).

## The 22 modules

### Core I/O

| Module | Description |
|--------|-------------|
| [filesystem](reference/filesystem.md) | 5 actions — Read, Write, Edit, Glob, Grep. Same surface as Claude Code. |
| [database](reference/database.md) | 16 actions — multi-driver async SQL with schema introspection, transactions, replicas, FK relations. |
| [shell](reference/shell.md) | 1 `Bash` tool with 5 modes (sync / async / status / kill / stdin-wait-stream). Git Bash on Windows. |
| [http](reference/http.md) | 16 actions — REST verbs, JSON API, multipart upload, background downloads, SSRF-guarded. |
| [web](reference/web.md) | 4 actions — search (5 backends, DuckDuckGo default), fetch, extract, download. |

### Agent intelligence

| Module | Description |
|--------|-------------|
| [memory](reference/memory.md) | 4 LLM-exposed actions — `Remember`, `SetGoal`, `TaskCreate`, `TaskUpdate`. Survives compaction. |
| [agent_spawn](reference/agent_spawn.md) | 1 `Agent` tool with 8 modes (spawn / wait / status / cancel / reassign / list / multi-wait). Background by default. |
| [behavior](reference/behavior.md) | Runtime enforcement — 14 built-in rules + 13 condition primitives + optional semantic classifier. **No agent-callable actions** — operates as a hook on the agent loop. |

### Knowledge

| Module | Description |
|--------|-------------|
| [vector](reference/vector.md) | 14 actions — vector collections, FastEmbed, Qdrant, hybrid search, 4 chunking strategies. |
| [rag](reference/rag.md) | 14 actions — knowledge bases, hybrid retrieval (BM25 + semantic + RRF), 6 backends, citations, semantic cache, Text2SQL, multi-query, CRAG. |
| [index_module](reference/index_module.md) | 7 internal actions — system module that auto-indexes the workspace for semantic code search. |

### Infrastructure

| Module | Description |
|--------|-------------|
| [queue](reference/queue.md) | 13 actions — async message queue (InMemory + Redis Streams), consumer groups, dead-letter, priorities, delays. |
| [cron_native](reference/cron_native.md) | 3 actions — `schedule`, `cancel_schedule`, `remind`. Tool-agnostic scheduler with natural-language delays. |

### UI

| Module | Description |
|--------|-------------|
| [workspace](reference/workspace.md) | 6 actions (`WsWrite` / `WsRead` / `WsEdit` / `WsGlob` / `WsGrep` / `WsDelete`) — virtual filesystem for live-canvas apps. |
| [preview](reference/preview.md) | 17 actions, **all internal** — Socket.IO transport for live preview UI (state, resources, ReactFlow nodes). |
| [widget](reference/widget.md) | 7 actions (`render`, `update`, `close`, `error`, `get_state`, `set_state`, `clear`) — declarative UI components for the Flutter / web client. |

### Integration

| Module | Description |
|--------|-------------|
| [mcp](reference/mcp.md) | 11 actions — connect external MCP servers (3 transports: stdio / sse / http), per-server sandbox permissions, OAuth flows, auto-reconnect. |
| [channels](reference/channels.md) | 11 actions + 11 adapters (webhook, cron, file_watcher, email, rss, log, queue, telegram, discord, slack, voice) — bidirectional I/O with full activation pipeline. |
| [lsp](reference/lsp.md) | 5 internal actions — universal real-time language feedback (LSP / compiler / linter), auto-detect, lazy startup, built-in fallback parsers. |

### System (auto-loaded, hidden from agents)

| Module | Description |
|--------|-------------|
| [context_builder](reference/context_builder.md) | 17 actions across 3 sub-files — tool discovery, system prompt assembly, execution routing, primitives (parallel / background / watchers / ask_user / call_app / use_skill). |
| [llm_provider](reference/llm_provider.md) | 6 internal actions — manages LLM provider instances, auto-configured from agent brains. `AgentBrain.backend` is `Literal["openai_compat", "anthropic", "github_copilot"]` — only these three values are valid. |
| [dev_tools](reference/dev_tools.md) | 3 ultra-powerful actions (`App`, `Chat`, `Run`) for testing and building apps against a live daemon. Used by the Builder agent. |

### Removed modules

| Removed | Replacement |
|---------|-------------|
| `workbench` | [workspace](reference/workspace.md) — same editing workflow but backed by a preview channel instead of internal buffers. |
| `git` | Use [shell](reference/shell.md) with native git commands (`Bash(command="git ...")`). |
| `notebook` | No direct replacement; use filesystem + shell. |
| `spreadsheet`, `pdf`, `hello` | Removed. |

## What is a module?

A module is a Python class that:

1. **Declares actions** with the `@action` decorator.
2. **Validates parameters** using Pydantic models.
3. **Returns structured results** via `ActionResult`.
4. **Manages its own lifecycle** via hooks (`on_start`,
   `on_config_update`, `on_stop`).
5. **Describes itself** via a TOML manifest. The framework
   uses this for discovery and documentation.

Modules are completely decoupled from each other — they
don't import or depend on other modules. The only shared
abstraction is `BaseModule` and `ActionResult`.

## Module anatomy

```
packages/digitorn/modules/<name>/
  digitorn-module.toml      # manifest: id, version, description, author
  __init__.py
  module.py                 # module class with @action methods
  params.py                 # Pydantic models for action parameters
  docs/
    actions.md              # action reference
    integration.md          # integration + config guide
```

## Creating a module

### 1. The manifest

`digitorn-module.toml`:

```toml
[module]
module_id          = "my_module"
module_class_path  = "digitorn.modules.my_module.module:MyModule"
version            = "1.0.0"
description        = "What this module does in one sentence."
author             = "Your Name"
isolation          = "shared"            # shared | isolated
platforms          = ["all"]
requirements       = []
tags               = ["category"]
```

| Field | Required | Description |
|-------|:--------:|-------------|
| `module_id` | ✓ | Unique id used in YAML and tool names. |
| `module_class_path` | ✓ | Python import path to the module class. |
| `version` | ✓ | Semantic version. |
| `description` | ✓ | One-line description shown in tool discovery. |
| `author` | | Author name. |
| `isolation` | | `shared` (default — one instance per app) or `isolated` (one per agent). |
| `platforms` | | `all`, `linux`, `macos`, `windows`. |
| `requirements` | | Python pip dependencies. |
| `tags` | | Categorisation tags for tool search. |

### 2. The module class

`module.py`:

```python
from __future__ import annotations
from typing import Any
from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action


class MyModule(BaseModule):
    MODULE_ID = "my_module"
    VERSION = "1.0.0"

    def __init__(self) -> None:
        super().__init__()
        self._connection = None

    async def on_start(self) -> None:
        """Called once when the module loads. Init resources here."""
        pass

    async def on_config_update(self, config: dict[str, Any]) -> None:
        """Called when the module receives its YAML config.

        Read config values; set up connections, file paths, API clients.
        """
        self._connection = config.get("connection_string")

    async def on_stop(self) -> None:
        """Called on unload. Clean up resources here."""
        self._connection = None

    @action(
        description="Describe what this action does clearly and concisely",
        params_model=MyActionParams,
        risk_level="low",
        tags=["category"],
    )
    async def my_action(self, params: MyActionParams) -> ActionResult:
        result = do_something(params.input)
        return ActionResult(success=True, data={"output": result})
```

### 3. Parameter models

`params.py`:

```python
from pydantic import BaseModel, Field


class MyActionParams(BaseModel):
    input: str = Field(
        ...,
        description="What this parameter does. Shown to the agent.",
    )
    optional_flag: bool = Field(
        default=False,
        description="Optional parameter with a default value.",
    )
```

> **Every field MUST have a `description`.** That string is
> what the agent sees when it discovers the tool.

### 4. Use it in YAML

```yaml
tools:
  modules:
    my_module:
      config:                                 # <-- mandatory wrapper
        connection_string: "sqlite:///data.db"
```

The module is auto-discovered from the manifest,
instantiated, configured via `on_config_update`, and its
actions are indexed for agent discovery.

> **Without the `config:` wrapper**, anything you put under
> the module block is silently dropped. This is the most
> common config bug. See [App Configuration → tools.modules](../app-language/02-app-config.md#toolsmodules--module-config).

## The `@action` decorator

```python
@action(
    description="What this action does",         # Required - shown to the agent.
    params_model=MyParams,                        # Pydantic input validator.
    risk_level="low",                             # low | medium | high
    permissions=["fs.read"],                      # Required permissions.
    tags=["io", "read"],                          # Categorisation + semantic search.
    aliases=["alternate_name", "autre_nom"],      # Multilingual aliases.
    side_effects=["filesystem_write"],            # Declared side effects.
    irreversible=False,                           # Warn before execution.
    require_approval=False,                       # Always require approval.
    data_classification="",                       # internal | confidential | public.
    platforms=None,                               # Restrict to specific platforms.
    examples=None,                                # Usage examples.
    streams_progress=False,                       # Streams progress updates.
    execution_mode="async",                       # async (default) | sync.
    cli_label="...",                              # Short label for the CLI.
    cli_param="...",                              # CLI primary param.
    internal=False,                               # If True, hidden from LLM schema.
)
async def my_action(self, params: MyParams) -> ActionResult:
    ...
```

### Risk levels

| Level | Meaning | Default policy |
|-------|---------|----------------|
| `low` | Read-only, no side effects. | Auto-approved. |
| `medium` | Local writes, reversible. | Depends on security profile. |
| `high` | Remote ops, irreversible, destructive. | Requires explicit grant. |

When `tools.capabilities` is declared, the security profile
maps risk levels to policies:

- `grant:` — auto-approved regardless of risk.
- `deny:` — blocked regardless of risk.
- `approve:` — requires user approval.
- Unmentioned → follows `default_policy` (`auto` / `approve`
  / `block`).

### Tags

Two purposes:

1. **Categorisation** in the tool index (agents can browse
   by tag).
2. **Semantic search** boost (tags are embedded alongside
   descriptions).

Common tags: `io`, `read`, `write`, `network`, `database`,
`git`, `search`, `dangerous`.

### Aliases

```python
@action(
    description="Read a file with line numbers",
    aliases=["lire", "cat", "voir", "afficher"],
)
```

The agent can search "lire un fichier" and find this
action.

### `internal=True`

Excludes the action from the LLM's tool schema entirely.
Used by system modules (`preview`, `lsp`, `index`) whose
actions are called by other modules via Python references,
never by the agent directly.

## ActionResult

Every action returns:

```python
ActionResult(
    success=True,                       # Required.
    data={"key": "value"},              # Structured result the agent sees.
    error=None,                         # Error when success=False.
    metadata={"cache_hit": True},       # Internal tracking, not shown to agent.
)
```

### Designing good results

The `data` dict is what the agent sees. Make it:

- **Structured** — named fields, not raw text blobs.
- **Actionable** — info the agent needs for its next step.
- **Bounded** — truncate large results to protect context.

❌ Bad:

```python
return ActionResult(success=True, data={"output": entire_file_contents})
```

✓ Good:

```python
return ActionResult(success=True, data={
    "path": str(path),
    "lines": line_count,
    "content": numbered_content,        # with line numbers for reference
    "truncated": was_truncated,
})
```

## Lifecycle hooks

```
Module loaded
  → on_start()
  → on_config_update(config)
  → [actions called]
  → on_stop()
```

| Hook | When | Use for |
|------|------|---------|
| `on_start()` | Module instantiated. | One-time init, resource allocation. |
| `on_config_update(config)` | YAML config applied. | Read config values; connect to services. |
| `on_stop()` | Module unloaded. | Close connections, flush buffers. |

> **Shared modules + per-app reconfig**: a `shared` module's
> `on_start()` runs ONCE at daemon boot with empty config.
> When an app activates, `on_config_update` is called with
> that app's config. The base `BaseModule.on_config_update`
> only stores the dict — to actually re-create resources
> (like the `rag` backend), override `on_config_update` and
> tear down + rebuild explicitly. See the gotcha in
> [rag → shared module + per-app reconfig](reference/rag.md#shared-module--per-app-reconfig-gotcha).

## Constraints

Apps can restrict module behaviour via `constraints:`:

```yaml
tools:
  modules:
    filesystem:
      constraints:
        paths: ["{{workdir}}"]              # only these directories
        max_file_size: "50MB"
        allowed_actions: [read, grep]       # whitelist actions
        blocked_actions: [write]            # blacklist
```

| Constraint | Scope | Description |
|------------|-------|-------------|
| `allowed_actions` | universal | Whitelist of allowed actions. |
| `blocked_actions` | universal | Blacklist of blocked actions. |
| `paths` | filesystem / workspace / shell | Restrict to specific directories. |
| `max_file_size` | filesystem | Max file size for read / write. |

Modules declare custom constraints via `ConstraintSpec` in
their `CONSTRAINTS` class attribute. The compiler validates
constraint keys against the module's declarations.

## How modules are discovered

1. The framework scans `packages/digitorn/modules/` for
   directories containing `digitorn-module.toml`.
2. Each TOML is parsed → `module_class_path` is loaded.
3. The module class is registered in the `ModuleRegistry`.
4. When YAML declares `tools.modules.<id>: {}`, the
   registry creates an instance.
5. The instance bootstraps: `on_start()` → `on_config_update(config)`.
6. The context builder indexes all actions from the
   instance's `_action_registry`.
7. Agents discover and execute actions via the tool
   discovery system.

No manual registration needed. Drop your module in the
right directory with a valid TOML and it works.

## Testing

Test by instantiating directly and calling actions:

```python
import asyncio
from my_module.module import MyModule
from my_module.params import MyActionParams


async def test_my_action():
    m = MyModule()
    await m.on_start()
    await m.on_config_update({"key": "value"})

    result = await m.my_action(MyActionParams(input="test"))

    assert result.success
    assert result.data["output"] == "expected"

    await m.on_stop()


asyncio.run(test_my_action())
```

For integration tests, use the full bootstrap pipeline so
the module runs inside the context builder + security
system. See `packages/digitorn/testing/README.md` for the
live-test SDK.

## Cross-references

- App-config block reference (`tools.modules`):
  [App Configuration → tools.modules](../app-language/02-app-config.md#toolsmodules--module-config)
- Tool injection (discovery vs direct):
  [Tool Injection](../app-language/04-tools.md)
- Behaviour engine (per-tool runtime checks):
  [Behavior Engine](../app-language/43-behavior.md)
- Hooks (agent-loop event hooks, separate mechanism):
  [hooks.md](../hooks.md)
- Middleware (LLM-call wrapping pipeline):
  [middleware.md](../middleware.md)
