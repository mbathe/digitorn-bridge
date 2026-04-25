---
id: modules-index
---

# Modules

Modules are the building blocks of agent capabilities in Digitorn. Each module provides a set of **actions** (tools) that agents can discover and execute at runtime.

A module is self-contained: it declares its own actions, parameters, risk levels, permissions, and lifecycle hooks. The framework handles discovery, routing, security enforcement, and context injection automatically.

## Available Modules

### Core I/O

| Module | Description |
|--------|-------------|
| [filesystem](reference/filesystem.md) | Agent-optimized file operations with line numbers, surgical edits, fast grep |
| [database](reference/database.md) | SQLite, PostgreSQL, MySQL with schema introspection and audit logging |
| [shell](reference/shell.md) | Execute shell commands (Git Bash on Windows), scripts, background tasks |
| [http](reference/http.md) | Full HTTP client with JSON API, form submission, file upload/download |
| [web](reference/web.md) | Web search (DuckDuckGo/Brave/Tavily), fetch, parse HTML to text |

### Agent Intelligence

| Module | Description |
|--------|-------------|
| [memory](reference/memory.md) | Cognitive memory: goals, plans, tasks, notes, facts, checkpoints |
| [agent_spawn](reference/agent_spawn.md) | Multi-agent orchestration: spawn, monitor, collect results (1 tool, 8 modes) |

### Infrastructure

| Module | Description |
|--------|-------------|
| [queue](reference/queue.md) | Event-driven message queue — Redis Streams, consumer groups, dead-letter |
| [vector](reference/vector.md) | Vector collections — FastEmbed, Qdrant, hybrid search |
| [cron_native](reference/cron_native.md) | Enterprise scheduler — 7-field cron, DAG dependencies, holidays, retry |
| [rag](reference/rag.md) | RAG knowledge bases with Qdrant backend |

### UI / Preview

| Module | Description |
|--------|-------------|
| [workspace](reference/workspace.md) | Virtual filesystem for live-preview apps (WsWrite, WsRead, WsEdit, WsGlob, WsGrep, WsDelete) — replaces the removed workbench |
| [preview](reference/preview.md) | Socket.IO transport for live preview UI (17 actions, all `internal=True`) |
| [widget](reference/widget.md) | Declarative Flutter UI components (render, update, close, state) |

### Integration

| Module | Description |
|--------|-------------|
| [mcp](reference/mcp.md) | Connect external MCP servers with normalization, cache, and middleware |
| [channels](reference/channels.md) | Output delivery (Slack, Telegram, email, webhook) + input adapters (webhook, file_watcher, telegram bot) |
| [lsp](reference/lsp.md) | Language Server Protocol — diagnostics via pyright/ruff/eslint + built-in fallback parsers |

### System (auto-loaded, not declared in YAML)

| Module | Description |
|--------|-------------|
| [context_builder](reference/context_builder.md) | Tool discovery engine, ask_user, use_skill, system prompt generation |
| [llm_provider](reference/llm_provider.md) | LLM provider management, auto-configuration from brain definitions |
| [index](reference/index_module.md) | Workspace indexing for semantic code search |

System modules are loaded automatically. They are hidden from agents and cannot be called directly. They provide the infrastructure that makes everything else work.

### Removed modules

The following modules have been **removed** and their functionality replaced:

- **workbench** → replaced by [workspace](reference/workspace.md). The workspace module provides 6 actions (write, read, edit, glob, grep, delete) with the same editing workflow but backed by a preview channel instead of internal buffers.
- **git** → use [shell](reference/shell.md) with native git commands (`shell.bash(command="git ...")`).
- **notebook** → removed (no direct replacement; use filesystem + shell).
- **spreadsheet**, **pdf**, **hello** → removed.

---

## What Is a Module?

A module is a Python class that:

1. **Declares actions** using the `@action` decorator. Each action is a tool the agent can call.
2. **Validates parameters** using Pydantic models. The agent sends structured input, the module validates it.
3. **Returns structured results** via `ActionResult`. The agent always gets `success`, `data`, or `error`.
4. **Manages its own lifecycle** via hooks (`on_start`, `on_config_update`, `on_stop`).
5. **Describes itself** via a TOML manifest. The framework uses this for discovery and documentation.

Modules are completely decoupled from each other. They don't import or depend on other modules. The only shared abstraction is `BaseModule` and `ActionResult`.

---

## Module Anatomy

Every module follows this structure:

```
packages/digitorn/modules/<name>/
  digitorn-module.toml    # manifest: id, version, description, author
  __init__.py
  module.py               # module class with @action methods
  params.py               # Pydantic models for action parameters
  docs/
    actions.md            # action reference documentation
    integration.md        # integration and configuration guide
```

---

## Creating a Module

### Step 1: The manifest

Create `digitorn-module.toml` in your module directory:

```toml
[module]
module_id = "my_module"
module_class_path = "digitorn.modules.my_module.module:MyModule"
version = "1.0.0"
description = "What this module does in one sentence."
author = "Your Name"
isolation = "shared"
platforms = ["all"]
requirements = []
tags = ["category"]
```

| Field | Required | Description |
|-------|----------|-------------|
| `module_id` | Yes | Unique identifier. Used in YAML and tool names. |
| `module_class_path` | Yes | Python import path to the module class. |
| `version` | Yes | Semantic version string. |
| `description` | Yes | One-line description shown in tool discovery. |
| `author` | No | Author name. |
| `isolation` | No | `shared` (default) or `isolated`. Isolated modules get their own instance per agent. |
| `platforms` | No | Supported platforms: `all`, `linux`, `macos`, `windows`. |
| `requirements` | No | Python package dependencies. |
| `tags` | No | Categorization tags for tool search. |

### Step 2: The module class

Create `module.py`:

```python
from __future__ import annotations
from typing import Any
from digitorn.modules.base import ActionResult, BaseModule
from digitorn.modules.decorators import action
from digitorn.modules.manifest import ModuleManifest

class MyModule(BaseModule):
    MODULE_ID = "my_module"
    VERSION = "1.0.0"

    def __init__(self) -> None:
        super().__init__()
        self._connection = None

    async def on_start(self) -> None:
        """Called once when the module is loaded. Initialize resources here."""
        pass

    async def on_config_update(self, config: dict[str, Any]) -> None:
        """Called when the module receives its YAML configuration.

        This is where you read config values and set up connections,
        file paths, API clients, etc.
        """
        self._connection = config.get("connection_string")

    async def on_stop(self) -> None:
        """Called when the module is unloaded. Clean up resources here."""
        self._connection = None

    @action(
        description="Describe what this action does clearly and concisely",
        params_model=MyActionParams,
        risk_level="low",
        tags=["category"],
    )
    async def my_action(self, params: MyActionParams) -> ActionResult:
        """Action implementation."""
        result = do_something(params.input)
        return ActionResult(success=True, data={"output": result})
```

### Step 3: Parameter models

Create `params.py`:

```python
from pydantic import BaseModel, Field

class MyActionParams(BaseModel):
    input: str = Field(
        ...,
        description="What this parameter does. This description is shown to the agent.",
    )
    optional_flag: bool = Field(
        default=False,
        description="Optional parameter with a default value.",
    )
```

Every field **must** have a `description`. This is what the agent sees when it discovers the tool. Clear descriptions lead to correct tool usage.

### Step 4: Use it in a YAML app

```yaml
modules:
  my_module:
    config:
      connection_string: "sqlite:///data.db"
```
The module is auto-discovered from the manifest, instantiated, configured, and its actions are indexed for agent discovery.

---

## The @action Decorator

The `@action` decorator registers a method as an agent-callable tool. Every parameter affects how the action appears in tool discovery and how the security system treats it.

```python
@action(
    description="What this action does",      # Required. Shown to the agent.
    params_model=MyParams,                     # Pydantic model for input validation.
    risk_level="low",                          # "low", "medium", "high"
    permissions=["fs.read"],                   # Required permissions (for security profile).
    tags=["io", "read"],                       # Categorization tags for semantic search.
    aliases=["alternate_name", "autre_nom"],   # Alternative names (multilingual support).
    side_effects=["filesystem_write"],         # Declared side effects.
    irreversible=False,                        # True = warn before execution.
    require_approval=False,                    # True = always require approval.
    data_classification="",                    # "internal", "confidential", "public".
    platforms=None,                            # Restrict to specific platforms.
    examples=None,                             # Usage examples for documentation.
    streams_progress=False,                    # True = action streams progress updates.
    execution_mode="async",                    # "async" (default) or "sync".
)
async def my_action(self, params: MyParams) -> ActionResult:
    ...
```

### Risk Levels

Risk levels control how the security system treats the action:

| Level | Meaning | Default Policy |
|-------|---------|---------------|
| `low` | Read-only, no side effects | Auto-approved |
| `medium` | Local writes, reversible changes | Depends on security profile |
| `high` | Remote operations, irreversible, destructive | Requires explicit grant |

When an app declares a `capabilities:` block, the security profile maps risk levels to policies:
- `grant:` actions are auto-approved regardless of risk
- `deny:` actions are blocked regardless of risk
- `approve:` actions require user approval before execution
- Unmentioned actions follow the `default_policy` (auto/approve/block)

### Tags

Tags serve two purposes:
1. **Categorization** in the tool index (agents can browse by tag)
2. **Semantic search** enhancement (tags are embedded alongside descriptions)

Common tags: `io`, `read`, `write`, `network`, `database`, `git`, `search`, `dangerous`.

### Aliases

Aliases provide alternative names for an action. Useful for multilingual support:

```python
@action(
    description="Read a file with line numbers",
    aliases=["lire", "cat", "voir", "afficher"],
)
```

The agent can search for "lire un fichier" and find this action.

---

## ActionResult

Every action returns an `ActionResult`:

```python
ActionResult(
    success=True,                    # Required. Did the action succeed?
    data={"key": "value"},           # Structured result data. The agent sees this.
    error=None,                      # Error message if success=False.
    metadata={"cache_hit": True},    # Internal metadata (not shown to agent by default).
)
```

| Field | Type | Description |
|-------|------|-------------|
| `success` | `bool` | Whether the action completed successfully. |
| `data` | `dict` or `None` | Structured result. Design this for the agent, not for humans. |
| `error` | `str` or `None` | Error message when `success=False`. Be specific and actionable. |
| `metadata` | `dict` | Internal tracking (timing, cache info). Not visible to the agent. |

### Designing Good Results

The `data` dict is what the agent sees. Design it to be:

- **Structured** -- use named fields, not raw text blobs
- **Actionable** -- include information the agent needs for its next step
- **Bounded** -- truncate large results to avoid filling the context window

Bad:
```python
return ActionResult(success=True, data={"output": entire_file_contents})
```

Good:
```python
return ActionResult(success=True, data={
    "path": str(path),
    "lines": line_count,
    "content": numbered_content,      # with line numbers for reference
    "truncated": was_truncated,
})
```

---

## Lifecycle Hooks

Modules have three lifecycle hooks:

```
Module loaded -- on_start() -- on_config_update(config) -- [actions called] -- on_stop()
```

| Hook | When | Use For |
|------|------|---------|
| `on_start()` | Module is instantiated | One-time initialization, resource allocation |
| `on_config_update(config)` | YAML config is applied | Read config values, connect to services, set up state |
| `on_stop()` | Module is being unloaded | Close connections, flush buffers, release resources |

### Config from YAML

The `config` dict in `on_config_update` comes from the YAML:

```yaml
modules:
  my_module:
    config:
      database: "sqlite:///data.db"
      timeout: 30
```
```python
async def on_config_update(self, config: dict[str, Any]) -> None:
    self._database = config.get("database", ":memory:")
    self._timeout = config.get("timeout", 30)
```

---

## Constraints

Applications can restrict what a module can do via constraints in the YAML:

```yaml
modules:
  filesystem:
    constraints:
      paths: ["{{workspace}}"]          # only these directories
      max_file_size: "50MB"             # file size limit
      allowed_actions: [read, grep] # only these actions
      blocked_actions: [write]      # never these actions
```
| Constraint | Scope | Description |
|-----------|-------|-------------|
| `allowed_actions` | Universal | Whitelist of allowed actions |
| `blocked_actions` | Universal | Blacklist of blocked actions |
| `paths` | filesystem | Restrict to specific directories |
| `max_file_size` | filesystem | Maximum file size for read/write |

Modules can declare custom constraints via `ConstraintSpec` in their manifest. The compiler validates constraint keys against the module's declarations.

---

## How Modules Are Discovered

1. The framework scans `packages/digitorn/modules/` for directories containing `digitorn-module.toml`
2. Each TOML is parsed to get the `module_class_path`
3. The module class is imported and registered in the `ModuleRegistry`
4. When an app's YAML declares `modules: {my_module: {}}`, the registry creates an instance
5. The instance is bootstrapped: `on_start()` then `on_config_update(config)`
6. The context builder indexes all actions from the instance's `_action_registry`
7. Agents discover and execute actions via the tool discovery system

No manual registration needed. Place your module in the right directory with a valid TOML and it works.

---

## Testing

Test modules by instantiating them directly and calling actions:

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

For integration tests, use the full bootstrap pipeline to verify the module works within the context builder and security system.
