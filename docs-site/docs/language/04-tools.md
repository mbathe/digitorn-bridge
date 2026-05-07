---
id: tools
---

# Tools

Digitorn agents call tools through a discovery architecture. The
`context_builder` module
 builds the tool index
at bootstrap, the runtime picks one of three injection modes based
on toolset size, and the LLM either receives full schemas, compact
listings, or a small set of meta-tools that let it discover the
rest on demand.

## Adaptive tool injection

 `_choose_tool_injection`.
The mode is picked **per agent at bootstrap** based on the brain's
context window vs the actual JSON size of every tool schema.

### The algorithm

```python
# bootstrap.py
budget         = context_window * 0.20            # _MAX_CONTEXT_RATIO
tool_tokens    = sum(len(json.dumps(t)) // 4 for t in tools)
                 # fallback: total_tools * 200 when direct_tools is empty
                 # (_FALLBACK_TOKENS_PER_TOOL)
compact_tokens = total_tools * 30                 # name + one-liner per tool

if tool_tokens <= budget:
    return "direct"          # full schemas
elif compact_tokens <= budget:
    return "compact_direct"  # names + descriptions
else:
    return "discovery"       # meta-tools
```

The result is stored on `AgentContext.tool_injection` and reused for
every turn. To force a specific mode, set
`runtime.tool_injection: direct | compact_direct | discovery` in the
YAML (`schema.py`); the algorithm is skipped and the forced
mode is used.

### Direct mode

Full OpenAI-compatible tool schemas are passed to the LLM - name,
description, complete `parameters` JSON schema, examples. The LLM
calls tools by name with full parameter knowledge.

Best for apps with ~1-3 modules and small total tool counts (every
tool fits comfortably in 20% of the context window).

### Compact direct mode

Each tool is listed by name + one-line description (~30 tokens
each). The LLM knows **which tools exist** and can call them
directly, but discovers the parameter schema at call time (the
runtime fetches it lazily).

Best for apps with 5-12 modules and 60-400 tools.

### Discovery mode

Domain tools are hidden behind **meta-tools**. The agent sees
strategic tools directly and discovers domain tools via semantic
search.

**Always direct (meta-tools, generated from
`actions_meta.py` `@action` registry, 9 entries)**:

| Action |
|--------|
| `search_tools` |
| `get_tool` |
| `execute_tool` |
| `list_categories` |
| `browse_category` |
| `run_parallel` |
| `use_skill` |
| `call_app` |
| `ask_user` |

**Always direct (background primitive)**:

| Action |
|--------|
| `background_run` |

**Conditionally direct** (added to the agent's tool index based on
the YAML config):

| Action set | Module | Gated by |
|------------|--------|----------|
| Memory tools | `memory` | Module is loaded under `tools.modules.memory` |
| Agent spawn (`Agent` tool) | `agent_spawn` | Agent's role is `coordinator` or `agent_spawn` is granted |
| Skills (`use_skill` already in meta - plus per-skill ergonomic shortcuts) | bundle `skills/` | Skills are declared under `dev.skills` |
| Watcher actions: `watch_start`, `watch_stop`, `watch_pause`, `watch_resume`, `watch_status`, `watch_list`, `watch_history` (7) | `context_builder.actions_watchers` | `runtime.watchers: true` |
| Scheduler actions: `schedule`, `cancel_schedule`, `remind` (3) | `cron_native` (`cron_native/module.py`) | `runtime.scheduler: true` |
| Channel notification helpers | `channels` | At least one channel declared in `tools.channels` |
| Workspace actions: `WsWrite`, `WsRead`, `WsEdit`, `WsGlob`, `WsGrep`, `WsDelete` (6) | `workspace` | Module is loaded under `tools.modules.workspace` |
| Direct modules | every action of every module listed | `runtime.direct_modules: [name, ...]` |

The remaining domain tools (filesystem, database, web, http, lsp,
etc.) sit behind the meta-tools and are reached via
`search_tools` / `browse_category` / `get_tool` / `execute_tool`.

Best for apps with MCP servers, plugin ecosystems, or 400+ tools.

### Threshold reference

The thresholds are deterministic given a context window and the
actual tool sizes. With the fallback estimator (200 tokens per
tool):

| Context window | Direct (≤N tools) | Compact (≤N tools) | Discovery (>N tools) |
|---------------:|------------------:|-------------------:|---------------------:|
| 8 K | 8 | 53 | 54+ |
| 32 K | 32 | 213 | 214+ |
| 60 K | 60 | 400 | 401+ |
| 128 K | 128 | 853 | 854+ |
| 200 K | 200 | 1 333 | 1 334+ |

When `direct_tools` is non-empty, the runtime uses the **actual JSON
size** of every tool schema (4 chars ≈ 1 token), so a small toolset
with very long descriptions can still tip into compact mode.

## How discovery works

```python
list_categories()
# returns: ["filesystem", "database", "web", "lsp", ...]

browse_category(category="filesystem")
# returns: [{name: "filesystem.read", description: "...", risk: "low"}, ...]

get_tool(name="filesystem.read")
# returns: {full JSON schema, examples, side_effects, aliases, ...}

execute_tool(name="filesystem.read", params={"path": "/tmp/file.txt"})
# returns: {success: true, data: "file contents..."}
```

The semantic index is built at bootstrap from a rich corpus: action
FQN + description + tags + parameter names + side effects + aliases
(see [Semantic search](#semantic-search) below).

## Auto-routing direct calls

If the LLM calls a tool by its short name directly
(`filesystem.read({...})` instead of
`execute_tool(name="filesystem.read", params={...})`), the agent
loop transparently routes it through `execute_tool`. This happens
in every mode, so the same agent code works whether the LLM saw the
full schema, a compact listing, or only the meta-tools.

## Module declaration

Tools come from modules declared under `tools.modules`. Every entry
is a `ModuleBlock` (`schema.py`).

```yaml
tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
    database:
      config:
        timeout_seconds: 10
      setup:
        - action: connect
          params:
            connection_id: main
            driver: sqlite
            database: "{{workdir}}/data.db"
      constraints:
        allowed_actions: [fetch_results, list_tables]
        blocked_actions: [execute_query]
```

The full `ModuleBlock` field reference (`config`, `setup`,
`constraints`, `middleware`, `credential`) is in
[App Configuration → tools.modules](02-app-config.md#toolsmodules---module-configuration).

The 22 modules shipped by the daemon are listed in
[the index](/docs/language/#modules); per-module action references live
under [modules/reference/](../reference/modules/). `context_builder`
and `llm_provider` are auto-loaded - never declared.

To inspect any module's actions and parameter schemas from the CLI:

```bash
digitorn app schema <module_id>
```

(`cli/app.py` `schema` command - registered at line 256 of.)

## Tool constraints

Two universal keys on `ModuleBlock.constraints`:

```yaml
tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]   # whitelist
    database:
      constraints:
        blocked_actions: [execute_query, drop_table]   # blacklist
```

The `context_builder` builds the agent's tool index with these
constraints applied - blocked / non-allowed actions are **invisible**
to the LLM. They can still be called from `setup:` steps, hooks, and
channel pipelines because those run with the daemon's identity, not
the agent's.

Module-specific constraints (anything beyond `allowed_actions` /
`blocked_actions`) are validated against the module's
`ConstraintSpec` declarations.

## Native vs text-based tool calling

`AgentBrain.backend` is
`Literal["openai_compat", "anthropic", "github_copilot"]`
(in `schema.py`). The runtime auto-detects whether a provider
supports native tool calling, with a per-agent override via
`brain.native_tool_use`.

- **Native** (Anthropic, OpenAI, DeepSeek, Groq, Mistral, Together,
  Gemini, xAI, Cerebras, Perplexity, Fireworks): meta-tools and any
  direct tools are passed via the API `tools=` parameter; the LLM
  emits structured `tool_calls`. The system prompt contains workflow
  instructions only.
- **Text-based** (Ollama, LM Studio, vLLM): tool schemas are injected
  into the system prompt; tool calls are parsed from the LLM's text
  output by the multi-format recovery parser
  ([Agents → Tool-call recovery](03-agents.md#tool-call-recovery)).

Override per agent via `brain.native_tool_use: true | false`. See
[Agents → Native vs text-based tool calling](03-agents.md#native-vs-text-based-tool-calling).

### What the system prompt looks like

In **native mode**:

```
You are agent "<id>" (role: <role>).

You have access to N tools across M domains.

To find and use tools, you have these meta-tools:
- search_tools: Keyword + semantic search over the visible tool index
- get_tool: Full schema, metadata, and examples for one tool
- execute_tool: Execute a tool with parameters
- list_categories: List all available tool domains
- browse_category: Browse all tools in a specific domain (paginated)

Workflow:
1. Discover what's available (list or search)
2. Get the exact parameter schema before calling
3. Execute the tool with the correct parameters

[Your system_prompt from YAML]
```

In **text-based mode** the meta-tools' full JSON schemas are
appended after the workflow block, plus the per-message expected
output format (`<tool_call>{json}</tool_call>` or equivalent).

## Tool name sanitization

OpenAI-compatible APIs require function names to match
`^[a-zA-Z0-9_-]+$`. Digitorn uses dotted FQNs internally
(`filesystem.read`); the runtime sanitizes both directions:

- **Outbound** (to API): `filesystem.read` → `filesystem__read`
- **Inbound** (from API): `filesystem__read` → `filesystem.read`

YAML authors and module developers always write the dotted form;
the conversion is invisible.

## Semantic search

Discovery mode uses **hybrid search** combining a semantic index and
a keyword inverted index.

- **Semantic** - FastEmbed + Qdrant, multilingual model
  `paraphrase-multilingual-MiniLM-L12-v2` (384 dims). Supports ~50
  languages.
- **Keyword** - inverted index with prefix matching.
- **Hybrid scoring** - semantic score (×10 weight) + keyword boost
  (+2-3) for ranking.

The corpus indexed per tool: FQN + description + tags + parameter
names + side effects + aliases + synonym expansion. Aliases are
declared on the `@action` decorator
(`@action(aliases=["lire", "read file", ...])`), which makes
non-English search queries find the right tool.

## Execution primitives

`context_builder` exposes a small set of primitives that wrap any
module action.

| Category | Action(s) | Source | Gated by |
|----------|-----------|--------|----------|
| Parallel | `run_parallel` | `actions_meta.py` | always |
| Background | `background_run` (one action, five modes - see [Discovery mode](#discovery-mode)) | `actions_background.py` | always |
| Skills | `use_skill` | `actions_meta.py` | always |
| App-as-tool | `call_app` | `actions_meta.py` | always |
| Human-in-the-loop | `ask_user` | `actions_meta.py` | always |
| Watchers | `watch_start`, `watch_stop`, `watch_pause`, `watch_resume`, `watch_status`, `watch_list`, `watch_history` | `actions_watchers.py` | `runtime.watchers: true` |
| Scheduler | `schedule`, `cancel_schedule`, `remind` | `cron_native/module.py` | `runtime.scheduler: true` |
| Long-term memory | `remember` | `memory/module.py` | `memory` module loaded |

See [Execution Primitives](04c-primitives.md) for full parameters
and examples.

## Cross-references

- Module configuration block reference: [App Configuration → tools](02-app-config.md#tools---modules-capabilities-channels)
- Built-in tools (delegation, memory, todo): [Built-in Tools](04b-builtin-tools.md)
- MCP server integration: [MCP Servers](04d-mcp.md)
- Capabilities (grant / approve / deny): [Security](11-security.md)
- Per-module reference: [modules/index.md](../reference/modules/)
