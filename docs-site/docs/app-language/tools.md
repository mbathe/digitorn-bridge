---

id: tools
title: Tools
sidebar_position: 4
format: md
---


# Tools

Tools are actions exposed to the LLM agent. They map to LLMOS module actions or built-in capabilities.

## Module Tools

Each LLMOS module exposes a set of actions. Declare them in the `tools:` list:

```yaml
agent:
  tools:
    # Single action from a module
    - module: filesystem
      action: read_file

    # All actions from a module (omit action)
    - module: filesystem

    # Multiple specific actions
    - module: filesystem
      actions: [read_file, write_file, list_directory]

    # All actions except some
    - module: os_exec
      exclude: [kill_process]

    # Override description for the LLM
    - module: filesystem
      action: read_file
      description: "Read the contents of a source code file"
```

### Tool Definition Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `module` | string | `""` | Module ID (e.g., `filesystem`, `os_exec`) |
| `action` | string | `""` | Single action name |
| `actions` | list | `[]` | Subset of actions to include |
| `exclude` | list | `[]` | Actions to exclude (when including whole module) |
| `description` | string | `""` | Override description shown to LLM |
| `constraints` | object | `{}` | Execution constraints (see below) |

### Available Modules

The LLMOS daemon provides 20 modules with 284 actions:

| Module | Actions | Description |
| -------- | ------- | ----------- |
| `filesystem` | 14 | File read/write, directory operations, search, file info |
| `os_exec` | 9 | Shell commands, process management, environment variables |
| `memory` | 11 | Multi-backend memory (working, episodic, semantic, cognitive) |
| `database` | 13 | SQLite/PostgreSQL/MySQL - query, execute, schema inspection |
| `database_gateway` | 12 | Multi-connection database gateway with connection pooling |
| `api_http` | 17 | HTTP client, REST API calls, OAuth, webhooks, GraphQL |
| `browser` | 13 | Playwright-based web automation (navigate, click, fill, screenshot) |
| `gui` | 13 | Low-level GUI automation (mouse, keyboard, screenshots, windows) |
| `computer_control` | 9 | Semantic GUI control via vision (click by description, type into element) |
| `perception_vision` | 4 | Screen capture, OCR, UI element detection (OmniParser/Ultra) |
| `excel` | 42 | Excel workbook operations (read/write cells, charts, formatting, export) |
| `word` | 30 | Word document operations (create, edit, tables, images, export to PDF) |
| `powerpoint` | 25 | PowerPoint operations (slides, shapes, charts, transitions, export) |
| `iot` | 10 | IoT device management (MQTT, device control, sensor data) |
| `agent_spawn` | 7 | Sub-agent spawning and lifecycle management |
| `context_manager` | 5 | Context window budget, compression, state management |
| `window_tracker` | 8 | Active window tracking, window list, focus history |
| `module_manager` | 24 | Module lifecycle, hub install/uninstall, community modules |
| `recording` | 6 | Workflow recording and replay |
| `triggers` | 6 | Schedule, watch, and event-based trigger management |
| `security` | 6 | Security scanning, permission checks, audit operations |

In standalone mode (no daemon), only `filesystem` and `os_exec` are available. Connect to the daemon for all modules.

## Tool Constraints

Constrain what a tool can do:

```yaml
agent:
  tools:
    - module: filesystem
      action: write_file
      constraints:
        paths: ["{{workspace}}/src", "{{workspace}}/tests"]
        max_file_size: "1MB"
        read_only: false

    - module: os_exec
      action: run_command
      constraints:
        timeout: "30s"
        working_directory: "{{workspace}}"
        forbidden_commands: ["rm -rf /", "dd if=/dev/zero", "mkfs"]
        forbidden_patterns: ["sudo *"]

    - module: api_http
      action: http_request
      constraints:
        allowed_domains: ["api.github.com", "pypi.org"]
        max_response_size: "5MB"
        network: true
```

### Constraint Fields

| Field | Type | Description |
|-------|------|-------------|
| `timeout` | string | Max execution time (e.g., `"30s"`, `"5m"`) |
| `paths` | list[string] | Allowed filesystem paths |
| `max_file_size` | string | Max file size for read/write (e.g., `"1MB"`) |
| `network` | bool | Allow network access |
| `working_directory` | string | Force working directory |
| `forbidden_commands` | list[string] | Blocked shell commands |
| `forbidden_patterns` | list[string] | Blocked command patterns (glob) |
| `allowed_domains` | list[string] | Allowed network domains |
| `max_response_size` | string | Max response size |
| `read_only` | bool | Restrict to read-only operations |
| `rate_limit_per_minute` | int | Max calls per minute for this tool |
| `forbidden_tables` | list[string] | Blocked database tables |

## Built-in Tools

Built-in tools are available without declaring a module. They handle runtime coordination: delegation, messaging, memory, and task tracking.

```yaml
agent:
  tools:
    - builtin: delegate        # multi-agent delegation, synchronous (hierarchical only)
    - builtin: delegate_async  # multi-agent delegation, background (hierarchical only)
    - builtin: todo            # persistent task tracking
    - builtin: memory          # multi-level memory read/write
    - builtin: ask_user        # interactive user input
    - builtin: emit            # event bus publishing
    - builtin: send_message    # peer-to-peer messaging (P2P only)
```

| Builtin | Purpose | Availability |
|---------|---------|-------------|
| `ask_user` | Prompt user for input | Single-agent, coordinator |
| `todo` | Task tracking (add, update, complete, list) | All modes |
| `delegate` | Delegate task to another agent (sync) | **Hierarchical multi-agent only** |
| `delegate_async` | Delegate task in background + watch mode | **Hierarchical multi-agent only** |
| `emit` | Publish event to event bus | All modes (requires daemon) |
| `memory` | Multi-level memory (store, recall, search) | Requires `memory:` block |
| `send_message` | Direct message to another agent | **P2P communication only** |

> **Full reference:** See [Built-in Tools Reference](builtin-tools) for complete parameter schemas, return values, error cases, and integration examples.

## Top-Level Tools Block

Tools can also be defined at the top level, separate from the agent. They're merged with the agent's tools:

```yaml
# These are merged
tools:
  - module: filesystem
  - module: os_exec

agent:
  tools:
    - module: agent_spawn
      action: spawn_agent
```

The agent sees all tools from both blocks.

## How Tools Become LLM Functions

The `AppToolRegistry` converts tool definitions into LLM-compatible function schemas:

1. **Module action** → Look up the module's manifest for the action's parameter schema
2. **Built-in** → Use hardcoded schemas for ask_user, todo, etc.
3. **Resolution** → Generate a `ResolvedTool` with name, description, and JSON schema

The LLM sees tools as callable functions:

```
filesystem.read_file(path: string) -> Read file contents
filesystem.write_file(path: string, content: string) -> Write content to file
os_exec.run_command(command: string | list) -> Execute shell command
```
