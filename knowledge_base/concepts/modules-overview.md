---
id: modules-overview
title: "Modules Overview"
type: concept
keywords: [modules, actions, tools, discovery, filesystem, shell, web, database, memory, rag, mcp, workspace, preview, lsp, agent_spawn, context_builder, llm_provider, http, channels, widget, config, grant, isolation, shared, isolated]
related: [what-is-digitorn, app-structure, common-errors, secrets-credentials]
source: docs/
---

# Modules Overview

## What is a module

A module is a Python plugin that provides a set of **actions** (tools) an agent can call. Modules are the bridge between the LLM and the real world. Each module has:

- **MODULE_ID** -- unique identifier (e.g. `filesystem`, `shell`, `web`)
- **Actions** -- callable functions with typed parameters (Pydantic models)
- **Config model** -- optional Pydantic model for static configuration
- **Setup steps** -- optional bootstrap actions (e.g. connect to a database)
- **Constraints** -- optional runtime restrictions (e.g. allowed/blocked actions)
- **Isolation mode** -- `shared` (one instance for all apps) or `isolated` (one per app)

## Discovery API

### List all available modules

```
GET /api/discovery/modules
```

Response:
```json
{
  "modules": [
    {
      "id": "filesystem",
      "name": "Filesystem",
      "description": "Read, write, edit, search files",
      "action_count": 5,
      "actions": ["read", "write", "edit", "glob", "grep"]
    },
    {
      "id": "shell",
      "name": "Shell",
      "description": "Execute shell commands",
      "action_count": 1,
      "actions": ["bash"]
    }
  ]
}
```

### Get module details with action parameters

```
GET /api/discovery/modules/{module_id}
```

Response includes each action's parameters, types, descriptions, and whether they're required:
```json
{
  "id": "filesystem",
  "actions": [
    {
      "name": "read",
      "description": "Read a file",
      "params": [
        {"name": "path", "type": "string", "required": true},
        {"name": "start_line", "type": "integer", "required": false},
        {"name": "end_line", "type": "integer", "required": false}
      ]
    }
  ]
}
```

## Module categories

### I/O modules

#### `filesystem` -- file operations

Actions: `read`, `write`, `edit`, `glob`, `grep`

Tool names: Read, Write, Edit, Glob, Grep

```yaml
modules:
  filesystem: {}

capabilities:
  grant:
    - module: filesystem
      actions: [read, write, edit, glob, grep]
```

Features:
- Fuzzy edit matching (6 strategies including 85% SequenceMatcher)
- Read guards (large files require read before edit)
- Auto-adds path to read set after write
- Built-in validators (JSON, YAML, TOML, Python) run after every write/edit

#### `shell` -- command execution

Actions: `bash`, `bash_background`, `bash_status`

Tool names: Bash, BashBackground, BashStatus

```yaml
modules:
  shell: {}

capabilities:
  grant:
    - module: shell
      actions: [bash, bash_background, bash_status]
```

Features:
- Uses Git Bash on Windows (not PowerShell, not WSL)
- CWD persists between calls
- Background tasks with status polling
- 5 execution modes with progressive notifications

#### `web` -- web search and fetch

Actions: `search`, `fetch`, `extract`, `download`

Tool names: WebSearch, WebFetch, WebExtract, WebDownload

```yaml
modules:
  web:
    config:
      search:
        primary: duckduckgo

capabilities:
  grant:
    - module: web
      actions: [search, fetch, extract, download]
```

#### `http` -- HTTP requests

Actions: `get`, `post`, `put`, `delete`, `json_api`

```yaml
modules:
  http: {}

capabilities:
  grant:
    - module: http
      actions: [get, post, json_api]
```

### Data modules

#### `database` -- SQL databases

Actions: `connect`, `execute_query`, `fetch_results`, `list_tables`, `describe_table`, `get_schema`

```yaml
modules:
  database:
    setup:
      - action: connect
        params:
          connection_id: main
          driver: sqlite
          database: "{{workspace}}/data.db"
    constraints:
      allowed_actions: [fetch_results, list_tables, describe_table, get_schema]
      blocked_actions: [execute_query]

capabilities:
  grant:
    - module: database
      actions: [fetch_results, list_tables, describe_table, get_schema]
```

Supported drivers: `sqlite`, `postgresql`, `mysql`, `mariadb`, `mssql`.

#### `memory` -- working memory

Actions: `set_goal`, `remember`, `recall`, `forget`, `add_todo`, `update_todo`

Tool names: SetGoal, Remember, Recall, Forget, TodoAdd, TodoUpdate

```yaml
modules:
  memory:
    config:
      working_memory: true
      todo_list: true
      runtime:
        goal_guardian: true

capabilities:
  grant:
    - module: memory
      actions: [set_goal, remember, recall, forget, add_todo, update_todo]
```

Memory is session-scoped. The agent uses it to track:
- Goal (what it's working on)
- Facts (key discoveries, decisions)
- Todos (task checklist with status)

#### `rag` -- retrieval-augmented generation

Actions: `query`, `ingest`, `list_knowledge_bases`, `create_knowledge_base`

```yaml
modules:
  rag:
    config:
      backend:
        type: qdrant
        path: "{{workspace}}/.digitorn/rag"
```

IMPORTANT: Config must be under `config:` key (not directly under the module).

The rag module is `shared` (one instance for all apps). When an app is activated, `on_config_update()` reconfigures the backend.

#### `vector` -- vector search

Actions: `add`, `search`, `delete`, and 11 more — see the per-action cards.

### AI modules

#### `llm_provider` -- LLM configuration

Not directly called by agents. Provides the LLM connection layer configured per-agent in the `brain:` block. Supports named provider definitions for reuse across agents.

```yaml
modules:
  llm_provider:
    config:
      providers:
        fast:
          provider: deepseek
          model: deepseek-chat
          backend: openai_compat
          config:
            api_key: "{{secret.DEEPSEEK_API_KEY}}"
            base_url: "https://api.deepseek.com/v1"
        smart:
          provider: anthropic
          model: claude-sonnet-4-20250514
          backend: anthropic
          config:
            api_key: "{{secret.ANTHROPIC_API_KEY}}"

agents:
  - id: main
    brain:
      provider_id: smart           # reference named provider
      temperature: 0.2
  - id: explore
    brain:
      provider_id: fast            # different provider for sub-agents
      temperature: 0.0
```

#### `agent_spawn` -- sub-agents

Actions: `agent` (spawn), `agent_wait`, `agent_wait_all`, `agent_result`, `agent_status`, `agent_cancel`, `agent_list`

Tool names: Agent, AgentWait, AgentWaitAll, AgentResult, AgentStatus, AgentCancel, AgentList

```yaml
capabilities:
  grant:
    - module: agent_spawn
      actions: [agent]
```

The coordinator agent spawns specialist sub-agents to work in parallel. Shared modules (`memory`, `web`, `lsp`, `filesystem`, `shell`) are reused -- sub-agents see the same workspace and memory.

#### `context_builder` -- meta tools

Actions: `ask_user`, `set_reminder`

Tool names: AskUser, SetReminder

```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [ask_user]
```

`ask_user` pauses the agent and asks the user a question via an approval queue.

### Integration modules

#### `mcp` -- Model Context Protocol

Connect to external MCP servers for additional tools:

```yaml
modules:
  mcp:
    config:
      servers:
        github:
          command: npx @modelcontextprotocol/server-github
          env:
            GITHUB_TOKEN: "{{secret.GITHUB_TOKEN}}"
          sandbox:
            permissions: [process.exec, net.http]
            allowed_hosts: [api.github.com]
```

MCP servers are admin-only. Regular users attach their own credentials via the credential store.

#### `channels` -- output delivery

Types: `webhook`, `slack`, `telegram`, `email`, `sms`, `kafka`, `log`

```yaml
channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{secret.SLACK_WEBHOOK}}"

  email_reports:
    type: email
    config:
      smtp_host: "smtp.gmail.com"
      from_address: "bot@example.com"
    user_resolver:
      module: database
      action: fetch_results
      params:
        query: "SELECT email FROM users WHERE session_id = :session_id"
      mapping:
        to_address: email
```

### Dev tools modules

#### `lsp` -- language server diagnostics

Actions: `diagnostics`, `notify_change`

```yaml
modules:
  lsp:
    config:
      python: "ruff check --output-format=json"
```

Built-in validators (no external tools needed): JSON, YAML, TOML, Python syntax. LSP servers (ruff, eslint, etc.) are tried first, then built-in fallback.

#### `index` -- semantic code search

Actions: `search`, `index`

### UI modules

#### `workspace` -- virtual file system

Actions: `write`, `read`, `edit`, `glob`, `grep`, `delete`

Tool names: WsWrite, WsRead, WsEdit, WsGlob, WsGrep, WsDelete

```yaml
modules:
  workspace:
    config:
      render_mode: react
      entry_file: src/App.tsx
      title: "My App"
      sync_to_disk: false
      lint: true
      instructions: |
        You are building a React app...
```

The agent writes virtual files; the client renders them in real time via SSE. Same API as filesystem -- the agent doesn't know files are virtual.

#### `preview` -- SSE transport layer

All 17 actions are `internal: true` (invisible to agents). The workspace module calls them as Python methods. Handles: state management, resource channels, event emission.

#### `widget` -- declarative UI

Agents can render and update UI widgets in 4 zones: inline (in chat), chat_side (companion panel), workspace_tabs, modals.

## How to configure modules

### In app.yaml

```yaml compile=skip
modules:
  {module_id}:
    config: { ... }              # static config pushed at bootstrap
    setup:                       # actions executed at bootstrap
      - action: {action_name}
        params: { ... }
    constraints:                 # runtime restrictions
      allowed_actions: [...]
      blocked_actions: [...]
    middleware:                   # per-module middleware
      - audit: { log_params: true }
```

### How to grant access

Agents can only use modules and actions explicitly granted in `capabilities:`:

```yaml
capabilities:
  default_policy: block
  grant:
    - module: filesystem
      actions: [read, write, edit, glob, grep]
    - module: shell
      actions: [bash]
    - module: memory                # empty actions = all actions
```

If `capabilities` is omitted entirely, no security enforcement is applied (dev/test mode).

### Hiding modules and actions

```yaml
capabilities:
  hidden_modules: [preview]           # loaded but invisible to agent
  hidden_actions:
    - module: database
      actions: [execute_query]        # invisible but still usable by hooks/setup
```

Hidden is different from denied: hidden actions are invisible but still executable by setup steps, hooks, and channels.

## Module isolation

- **`shared`** -- one instance for the entire daemon. All apps share it. Example: `rag` (the store is on disk — sharing is intentional so multiple apps can see the same KBs). Config is per-app via `on_config_update()`.

- **`session`** -- one instance per session (this is what most modules use in the current code). Each session gets its own state. Examples: `filesystem`, `memory`, `database`, `workspace`. This is the default.

- **`isolated`** -- reserved for future use; not currently in the code.

Sub-agents share certain modules with their parent: `memory`, `web`, `lsp`, `filesystem`, `shell`. This ensures they see the same workspace, CWD, and read files set.

## Tool name resolution

All tools use SHORT names like Claude Code: `Write`, `Read`, `Edit`, `Bash`, `Grep`, `Glob`, `Agent`, `Remember`, `TaskCreate`, `TaskUpdate`, etc.

Centralized mapping in `tool_names.py`:
- `to_fqn("Write")` returns `"filesystem.write"`
- `to_short("filesystem.write")` returns `"Write"`
- Modules without explicit mapping get auto-generated PascalCase names

## See also

- what-is-digitorn -- the big picture
- app-structure -- where to put module config
- common-errors -- module/action not found errors
- secrets-credentials -- configuring module API keys
