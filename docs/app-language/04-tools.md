---
id: tools
---

# Tools

Digitorn uses a **tool discovery architecture**. Instead of exposing all tools directly to the LLM (which wastes context tokens), agents discover and execute tools through meta-tools.

## Adaptive Tool Injection

Digitorn automatically chooses between **three** injection modes based on toolset size vs context window. The decision is made at bootstrap and stored in `AgentContext.tool_injection`.

### Decision Logic

```
budget = context_window × 20%

If total_tools × 200 tokens ≤ budget  →  direct         (full schemas)
If total_tools × 30 tokens  ≤ budget  →  compact_direct (names + descriptions)
Otherwise                              →  discovery      (5 meta-tools)
```

### Direct Mode (small toolsets)

**When:** Full JSON schemas fit in ≤20% of context (e.g. 60 tools with 60K context).

All tools are passed as complete OpenAI function schemas - name, description, parameters with types and descriptions, examples. The agent calls tools by name with full parameter knowledge.

```
tools: [hello__greet, filesystem__read, filesystem__ls, filesystem__grep, ...]
```

Best for: apps with 1-3 modules and &lt;60 total tools.

### Compact Direct Mode (medium toolsets)

**When:** Full schemas don't fit, but tool names + one-liners do (e.g. 136 tools with 60K context).

All tools are listed by name and short description (~30 tokens each). No full parameter schemas. The agent knows **which tools exist** and can call them directly, but discovers parameter details at call time.

```
tools: 136 tools listed by name + description (compact format)
```

Best for: apps with many modules (5-12) and 60-400 total tools. This is the typical mode for a full-featured coding assistant like OpenCode.

### Discovery Mode (large toolsets)

**When:** Even compact listing exceeds 20% of context (400+ tools).

Domain tools are hidden behind **5 meta-tools** and discovered via semantic search. But the agent still sees **strategic tools directly** - these are always injected because the agent needs them for reasoning, not domain work.

**Always direct (meta-tools):**
- `search_tools`, `get_tool`, `execute_tool`, `list_categories`, `browse_category`

**Always direct (primitives):**
- `run_parallel`, `background_run/status/result/cancel/list/wait`

**Conditionally direct (based on YAML config):**
- **Memory** (16 actions) - if `memory` module is loaded
- **Agent spawn** (7 actions) - if agent role is `coordinator`
- **Skills** (`use_skill`) - if skills are declared
- **Watchers** (7 actions) - if `watchers: true`
- **Scheduler** (6 actions) - if `scheduler: true`
- **Channels** (`send_notification`) - if channels configured
- **Workspace** (6 actions) - if `workspace` module is loaded (WsWrite, WsRead, WsEdit, WsGlob, WsGrep, WsDelete)
- **Direct modules** - all actions from modules listed in `execution.direct_modules`

The remaining domain tools (filesystem, database, web, etc.) are discovered via semantic search.

Best for: apps with MCP servers, plugin ecosystems, or 400+ total tools.

### Thresholds by Context Window

| Context Window | Direct (≤N tools) | Compact (≤N tools) | Discovery (>N tools) |
|---------------|-------------------|-------------------|---------------------|
| 8K | 8 | 53 | 54+ |
| 32K | 32 | 213 | 214+ |
| 60K | 60 | 400 | 401+ |
| 128K | 128 | 853 | 854+ |
| 200K | 200 | 1333 | 1334+ |

## How Discovery Mode Works

The `context_builder` module indexes all tools from loaded modules and exposes them through meta-tools. The agent never sees the full tool list - it searches, browses, and executes tools on demand.

```
1. Agent calls list_categories()
   -> Returns: ["hello", "filesystem", "database"]

2. Agent calls browse_category(category="filesystem")
   -> Returns: [{ name: "filesystem.read", description: "Read a file", ... }, ...]

3. Agent calls get_tool(name="filesystem.read")
   -> Returns: { full JSON schema, examples, side effects }

4. Agent calls execute_tool(name="filesystem.read", params={"path": "/tmp/file.txt"})
   -> Returns: { success: true, data: "file contents..." }
```

## Meta-Tools

The meta-tools are defined via `@action` decorators in the `context_builder` module. They are generated **dynamically** from the registry - adding a new `@action` makes it available everywhere automatically.

Current meta-tools:

| Meta-Tool | Description | Key Params |
| --------- | ----------- | ---------- |
| `search_tools` | Keyword search over all visible tools | `query` (str), `max_results` (int, 1-20) |
| `get_tool` | Full schema and metadata for one tool | `name` (str, "module.action" format) |
| `execute_tool` | Execute a tool with parameters | `name` (str), `params` (dict) |
| `list_categories` | List all available tool domains | (none) |
| `browse_category` | Browse tools in a domain (paginated) | `category` (str), `page` (int), `page_size` (int) |

### Auto-Routing

If an LLM calls a tool directly (e.g., `filesystem.read` instead of `execute_tool(name="filesystem.read")`), the agent loop auto-routes it through `execute_tool`. This happens transparently for better LLM compatibility.

## Module Tools

Tools come from modules declared in the `modules:` block. Each module exposes actions via `@action` decorators.

### Declaring Modules

```yaml
modules:

  # Load with constraints (restrict available actions)
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

  # Load with config and setup
  database:
    config:
      timeout_seconds: 10
    setup:
      - action: connect
        params:
          driver: sqlite
          database: "{{workspace}}/data.db"
    constraints:
      allowed_actions: [fetch_results, list_tables]
```
### Currently Implemented Modules

| Module | Actions | Description |
| ------ | ------- | ----------- |
| `hello` | `say_hello`, `greet_many`, `status` | Simple greeting (test/demo) |
| `filesystem` | `read`, `ls`, `find`, `grep`, `write`, `mkdir`, ... | File operations |
| `database` | `connect`, `query`, `fetch_results`, `list_tables`, `upsert`, `batch_execute`, ... | Database operations |
| `http` | `get`, `post`, `json_api`, `fetch_page`, `head`, `download`, ... | HTTP client with async downloads |
| `shell` | `run`, `script`, `which`, `env`, `background_run`, `task_status`, ... | Shell command execution |
| `mcp` | `connect`, `disconnect`, `list_servers`, `call_tool`, `list_resources`, ... | MCP server integration (auto-indexes external tools) |

> Use `digitorn app schema {module_id}` to see all actions and their parameter schemas.
>
> MCP tools from connected servers appear as virtual modules (`mcp_slack`, `mcp_github`, etc.) - see [MCP Servers](04d-mcp.md) for details.

### Tool Constraints

Constrain what tools an agent can access:

```yaml
modules:
  filesystem:
    constraints:
      # Only these actions are visible to the agent
      allowed_actions: [read, glob, grep]

  database:
    constraints:
      # These actions are blocked
      blocked_actions: [execute_query, drop_table]
```
The `context_builder` applies these constraints when building the tool index - blocked actions are invisible to the agent.

## Native vs Text-Based Tool Use

Digitorn supports two modes of tool interaction, selected automatically based on the provider:

### Native Tool Use (OpenAI, DeepSeek, Groq, Mistral, Together)

- Meta-tools are passed via the API `tools=` parameter
- The LLM generates structured tool calls natively
- The system prompt contains workflow instructions only

### Text-Based Tool Use (Ollama, LM Studio, vLLM)

- Full tool schemas are injected into the system prompt
- The LLM generates tool calls as text:
  ```
  `{tool_call}`{"name": "list_categories", "arguments": {}}`</tool_call>`
  ```
- Digitorn parses tool calls from the LLM's text output using multiple strategies

### Overriding the Mode

The mode is auto-detected from the provider, but you can override it per agent via `brain.native_tool_use`:

```yaml
brain:
  provider: ollama
  model: qwen2.5-coder:7b
  native_tool_use: true   # Force native mode (this model supports it)
```
This is useful for local models like `qwen2.5-coder` that support the OpenAI tool calling format natively. See [Agent Configuration](03-agents.md#overriding-tool-calling-mode) for details.

### How the System Prompt is Built

For **native** mode:
```
You are agent "assistant" (role: assistant).

You have access to N tools across M domains.

To find and use tools, you have these 5 meta-tools:
- search_tools: Keyword search over all visible tools
- get_tool: Full schema, metadata, and examples for one tool
- execute_tool: Execute a tool with parameters
- list_categories: List all available tool domains
- browse_category: Browse all tools in a specific domain

Workflow:
1. Discover what's available (list or search)
2. Get the exact parameter schema before calling
3. Execute the tool with the correct parameters

[Your system_prompt from YAML]
```

For **text-based** mode:
```
You are agent "assistant" (role: assistant).

You have access to N tools across M domains.

# AVAILABLE TOOLS

To call a tool, output EXACTLY this XML format:
`{tool_call}`{"name": "tool_name", "arguments": {"param": "value"}}`</tool_call>`

## Tools
### search_tools
Keyword search over all visible tools
Parameters: { "query": ..., "max_results": ... }

### execute_tool
Execute a tool with parameters
Parameters: { "name": ..., "params": ... }
[... all meta-tools with full schemas ...]

[Your system_prompt from YAML]
```

## Tool Name Sanitization

OpenAI-compatible APIs require function names to match `^[a-zA-Z0-9_-]+$`. Since Digitorn uses dotted FQNs (e.g., `filesystem.read`), the runtime automatically sanitizes names:

- **Outbound** (to API): `filesystem.read` -- `filesystem__read` (dots replaced with double underscores)
- **Inbound** (from API): `filesystem__read` -- `filesystem.read` (reverse conversion before dispatch)

This is transparent - YAML authors and module developers always use the `module.action` format.

## Semantic Search

Tool discovery in discovery mode uses **hybrid search** combining:

- **Semantic search** (FastEmbed + Qdrant): Multilingual embeddings (`paraphrase-multilingual-MiniLM-L12-v2`, 384 dims) for meaning-based matching. Supports ~50 languages.
- **Keyword search**: Inverted index with prefix matching for exact term lookup.
- **Hybrid scoring**: Semantic score (×10 weight) + keyword boost (+2-3) for optimal ranking.

The semantic index is built at bootstrap from a rich corpus: FQN + description + tags + param names + side effects + aliases + synonym expansion.

### Module Aliases

Modules can declare **aliases** for their actions using the `@action(aliases=[...])` decorator. Aliases are indexed in both keyword and semantic indexes, improving discoverability in multiple languages.

Example: `filesystem.read` has aliases like `"lire"`, `"lire fichier"`, `"read file"` - so a French-speaking agent searching for "lire un fichier" will find it.

## Dynamic Architecture

The entire tool system is built from a single source of truth: the `context_builder` module's `@action` registry.

Adding a new meta-tool requires only:
1. Add an `@action` method to `context_builder/module.py`
2. Define a Pydantic params model

Everything else updates automatically:

- `bootstrap.py` generates the JSON schema via `action_entry_to_json_schema()`
- `prompt.py` generates system prompt instructions from the tools list
- `agent_loop.py` routes calls via the `_action_registry`
- `openai_compat.py` extracts tool names dynamically for recovery
- `ui.py` falls back to `"Calling {name}"` for any unknown tool

No hardcoded tool names anywhere in the pipeline.

## Execution Primitives

In addition to meta-tools, the `context_builder` provides **execution primitives** - capabilities for parallel execution, background tasks, persistent monitoring, and time-based scheduling:

| Category | Primitives | Requires |
|----------|-----------|----------|
| Parallel | `run_parallel` | Always available |
| Background | `background_run`, `background_status`, `background_result`, `background_cancel`, `background_list`, `background_wait` | Always available |
| Watchers | `watch_start`, `watch_stop`, `watch_pause`, `watch_resume`, `watch_status`, `watch_list`, `watch_history` | `execution.watchers: true` |
| Scheduler | `schedule_once`, `schedule_cron`, `schedule_cancel`, `schedule_list`, `schedule_status`, `remember` | `execution.scheduler: true` |

Parallel and background primitives are **always injected**. Watchers and scheduler primitives require opt-in via the `execution:` block. All primitives work with any module action and respect security policies.

See [Execution Primitives](04c-primitives.md) for full documentation.
