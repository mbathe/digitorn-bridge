---

id: sdk
title: SDK Reference
sidebar_position: 6
format: md
---



# SDK — LangChain Integration

The `langchain-llmos` package provides the agent-side SDK for LLMOS Bridge. It offers multi-provider autonomous agents, a reactive plan loop, auto-generated LangChain tools, HTTP clients, and safety rails.

---


## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │               langchain-llmos                │
                    └─────────────────────────────────────────────┘
                                        |
              ┌─────────────────────────┼─────────────────────────┐
              |                         |                         |
    ┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────┐
    │ ComputerUseAgent │    │   ReactivePlanLoop    │    │  LLMOSToolkit   │
    │ (top-level API)  │    │  (Plan→Exec→Observe)  │    │  (LangChain     │
    └─────────────────┘    └──────────────────────┘    │   tool gen)     │
              |                         |               └─────────────────┘
              v                         v                         |
    ┌─────────────────────────────────────────────┐              v
    │           Provider Abstraction               │    ┌─────────────────┐
    │                                              │    │ LLMOSActionTool │
    │  AgentLLMProvider (ABC)                      │    │ (BaseTool)      │
    │  ├── AnthropicProvider (Claude)              │    └─────────────────┘
    │  ├── OpenAICompatibleProvider (GPT/Ollama)   │
    │  ├── GeminiProvider (Google)                 │
    │  └── ProviderRegistry (YAML specs)           │
    └─────────────────────────────────────────────┘
                            |
              ┌─────────────┼─────────────┐
              |                           |
    ┌─────────────────┐        ┌─────────────────┐
    │  LLMOSClient    │        │ AsyncLLMOSClient │
    │  (sync HTTP)    │        │  (async HTTP)    │
    └─────────────────┘        └─────────────────┘
              |                           |
              └───────────┬───────────────┘
                          v
                ┌─────────────────┐
                │  LLMOS Bridge   │
                │  Daemon (REST)  │
                └─────────────────┘
```

---


## Package Exports

```python
from langchain_llmos import (
    # Agent
    ComputerUseAgent,
    AgentResult,
    StepRecord,

    # Reactive loop
    ReactivePlanLoop,

    # Toolkit
    LLMOSToolkit,
    LLMOSActionTool,

    # Clients
    LLMOSClient,
    AsyncLLMOSClient,

    # Safeguards
    SafeguardConfig,

    # Providers
    AgentLLMProvider,
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    ProviderRegistry,
    ProviderSpec,
    ModelSpec,
    ModelCapabilities,
    build_agent_provider,
    get_registry,
)
```

---


## ComputerUseAgent

Top-level API for autonomous computer use. Wraps provider selection, tool building, and execution loop.

### Constructor

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `provider` | str or AgentLLMProvider | None | Provider name or instance |
| `api_key` | str | None | API key for provider |
| `model` | str | None | Model name (provider default if None) |
| `base_url` | str | None | Override base URL |
| `supports_vision` | bool | None | Override vision detection |
| `daemon_url` | str | `http://127.0.0.1:40000` | Bridge daemon URL |
| `daemon_api_token` | str | None | Daemon bearer token |
| `max_tokens` | int | 4096 | Max tokens per LLM response |
| `system_prompt` | str | None | Custom prompt (auto-fetched if None) |
| `allowed_modules` | list[str] | DEFAULT_MODULES | Module IDs to expose |
| `max_steps` | int | 30 | Maximum tool-call iterations |
| `verbose` | bool | False | Print step-by-step progress |
| `approval_mode` | str | `"auto"` | `auto`, `always_reject`, or `callback` |
| `approval_callback` | ApprovalCallback | None | Async approval handler |

**Default modules**: `computer_control`, `gui`, `os_exec`, `filesystem`, `window_tracker`.

### Methods

| Method | Description |
|--------|-------------|
| `async run(task, max_steps, use_reactive_loop)` | Execute autonomous task |
| `async close()` | Close HTTP clients and provider |

**`run()` modes**:
- `use_reactive_loop=True` (default): Plan multiple steps, execute batch, observe, re-plan
- `use_reactive_loop=False`: Legacy 1-action-at-a-time tool calling loop

### AgentResult

| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | Task completion status |
| `output` | str | Final text output from LLM |
| `steps` | list[StepRecord] | All executed steps |
| `total_duration_ms` | float | Total execution time |

### StepRecord

| Field | Type | Description |
|-------|------|-------------|
| `tool_name` | str | e.g. `computer_control__read_screen` |
| `tool_input` | dict | Parameters passed |
| `tool_output` | dict or str | Result from daemon |
| `duration_ms` | float | Step execution time |

### Approval Modes

| Mode | Behavior |
|------|----------|
| `auto` | Auto-approve all approval requests |
| `always_reject` | Auto-reject all approval requests |
| `callback` | Call `approval_callback(plan_id, action_data)` for each request |

### Usage

```python
async with ComputerUseAgent(provider="anthropic") as agent:
    result = await agent.run("Open Firefox and navigate to github.com")
    print(result.output)
    print(f"Completed in {len(result.steps)} steps")
```

---


## ReactivePlanLoop

The core execution engine: Plan → Execute → Observe → Re-plan.

### How It Works

```
1. LLM receives task + system prompt + tool definitions
2. LLM outputs JSON plan (array of PlanStep)
3. Plan is submitted to daemon as a single IML plan
4. Daemon executes all steps (respecting dependencies)
5. Results assembled into observation text
6. Observation sent back to LLM
7. LLM decides: done → final answer, or re-plan → goto 2
```

### Constructor

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `provider` | AgentLLMProvider | required | LLM provider instance |
| `daemon` | AsyncLLMOSClient | required | Daemon client |
| `max_replans` | int | 3 | Maximum plan iterations |
| `max_steps_per_plan` | int | 8 | Max actions per plan |
| `max_total_actions` | int | 30 | Total action limit |
| `max_tokens` | int | 4096 | Max tokens per response |
| `verbose` | bool | False | Print progress |
| `safeguards` | SafeguardConfig | None | Safety configuration |
| `approval_mode` | str | `"auto"` | Approval mode |
| `session_config` | dict or None | None | Auto-create a session for each `run()` call (see below) |

### Session Lifecycle

When `session_config` is provided, `ReactivePlanLoop` automatically manages a daemon session for each `run()` call:

1. **Before the loop starts** — creates a session via `daemon.create_session(**session_config)` and injects the `session_id` into `daemon.session_id`
2. **During the loop** — all `submit_plan()` calls carry `X-LLMOS-Session` header automatically
3. **After the loop finishes** (success, failure, or exception) — deletes the session and clears `daemon.session_id`

Session creation/deletion failures are **soft** — they are logged (when `verbose=True`) but do not abort the run.

```python
loop = ReactivePlanLoop(
    provider=provider,
    daemon=AsyncLLMOSClient(app_id="myapp"),
    session_config={
        "app_id": "myapp",
        "expires_in_seconds": 3600,
        "allowed_modules": ["filesystem", "os_exec"],
        "permission_denials": ["os.environment.write"],
    },
)
result = await loop.run(task, system_prompt, tools)
# Session is created before the loop and deleted after.
```

`session_config` keys match `AsyncLLMOSClient.create_session()` keyword arguments: `app_id`, `agent_id`, `expires_in_seconds`, `idle_timeout_seconds`, `allowed_modules`, `permission_grants`, `permission_denials`.

### PlanStep

| Field | Type | Description |
|-------|------|-------------|
| `id` | str | Step identifier |
| `action` | str | `module__action_name` format |
| `params` | dict | Action parameters |
| `depends_on` | list[str] | Dependency step IDs |
| `description` | str | Human-readable description |

### SSE Streaming

The loop uses SSE streaming when `httpx-sse` is installed, with automatic fallback to polling:

```
_stream_plan(plan_id)
    |
    +--→ httpx-sse available → SSE /plans/{id}/stream
    |       event: action_progress → append to progress_log
    |       event: action_result_ready → store in action_results
    |       event: plan_completed/plan_failed → return final state
    |
    +--→ httpx-sse unavailable → _poll_plan() (0.5s interval)
```

### Observation Building

After plan execution, the loop builds an observation containing:
- Each action's result (truncated to 12,000 chars)
- Scene graph / UI elements (compacted to 50 elements max)
- Screenshots (resized to 1024px max, JPEG, last 2 kept in history)
- Iteration counter (e.g., "Iteration 2/3")

---


## Provider Abstraction

### AgentLLMProvider (ABC)

All providers implement this interface:

| Method | Description |
|--------|-------------|
| `async create_message(system, messages, tools, max_tokens)` | Send messages to LLM, get response |
| `format_tool_definitions(tools)` | Convert to provider-specific format |
| `build_user_message(text)` | Build user message block |
| `build_assistant_message(turn)` | Build assistant message from LLM turn |
| `build_tool_results_message(results)` | Build tool result messages |
| `supports_vision` (property) | Whether provider supports image input |
| `async close()` | Release resources |

### Data Types

#### ToolDefinition

| Field | Type | Description |
|-------|------|-------------|
| `name` | str | Tool name |
| `description` | str | Tool description |
| `parameters_schema` | dict | JSON Schema for parameters |

#### ToolCall

| Field | Type | Description |
|-------|------|-------------|
| `id` | str | Tool call ID |
| `name` | str | Tool name called |
| `arguments` | dict | Parsed arguments |

#### LLMTurn

| Field | Type | Description |
|-------|------|-------------|
| `text` | str or None | Text response |
| `tool_calls` | list[ToolCall] | Requested tool calls |
| `is_done` | bool | Whether agent is finished |
| `raw_response` | Any | Opaque SDK response |

#### ToolResult

| Field | Type | Description |
|-------|------|-------------|
| `tool_call_id` | str | Matching tool call ID |
| `text` | str | Text result |
| `image_b64` | str or None | Optional screenshot |
| `image_media_type` | str | Default `image/png` |
| `is_error` | bool | Whether result is an error |

### Built-in Providers

#### AnthropicProvider

| Parameter | Default | Description |
|-----------|---------|-------------|
| `api_key` | `ANTHROPIC_API_KEY` | API key |
| `model` | `claude-sonnet-4-20250514` | Model ID |

Vision: always True. Tool format: Anthropic native (`input_schema`).

#### OpenAICompatibleProvider

| Parameter | Default | Description |
|-----------|---------|-------------|
| `api_key` | `OPENAI_API_KEY` | API key |
| `model` | `gpt-4o` | Model ID |
| `base_url` | `https://api.openai.com/v1` | API endpoint |
| `vision` | True | Vision support |

Tool format: OpenAI function calling (`"type": "function"`). Compatible with Ollama, Mistral, and any OpenAI-compatible API.

#### GeminiProvider

| Parameter | Default | Description |
|-----------|---------|-------------|
| `api_key` | `GOOGLE_API_KEY` | API key |
| `model` | `gemini-2.5-flash` | Model ID |

Vision: always True. Tool format: Google generativeai `Tool(function_declarations=[...])`.

### ProviderRegistry

Declarative provider management via YAML specs.

| Method | Description |
|--------|-------------|
| `register(spec)` | Register provider spec |
| `register_class(provider_id, cls)` | Register provider class directly |
| `load_yaml(path)` | Load specs from YAML file |
| `load_builtins()` | Load built-in provider specs |
| `get_spec(provider_id)` | Get provider spec |
| `list_providers()` | List registered provider IDs |
| `has(provider_id)` | Check if provider exists |
| `build(provider_id, api_key, model, base_url, vision)` | Instantiate provider |

### ProviderSpec

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `provider_id` | str | required | Unique ID |
| `display_name` | str | `""` | Human label |
| `api_style` | Literal | `openai_compat` | `anthropic`, `openai_compat`, `google_genai`, `custom` |
| `provider_class` | str | None | Fully-qualified class path |
| `base_url` | str | None | API base URL |
| `env_key` | str | None | Environment variable for API key |
| `auth_method` | Literal | `bearer` | `bearer`, `api_key_header`, `none` |
| `sdk_package` | str | None | Required pip package |
| `default_model` | str | required | Default model ID |
| `models` | dict[str, ModelSpec] | `{}` | Available models |
| `rate_limit_rpm` | int | None | Rate limit (requests per minute) |
| `timeout_seconds` | int | 60 | Request timeout |

### ModelSpec

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model_id` | str | required | Model identifier |
| `display_name` | str | `""` | Human label |
| `max_input_tokens` | int | 128000 | Context window |
| `max_output_tokens` | int | 4096 | Max output |
| `capabilities` | ModelCapabilities | default | Vision, tool_use, streaming, etc. |
| `pricing` | ModelPricing | None | Cost per 1M tokens |

### ModelCapabilities

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `vision` | bool | False | Image input |
| `tool_use` | bool | True | Function calling |
| `streaming` | bool | True | Streaming responses |
| `audio_input` | bool | False | Audio input |
| `video_input` | bool | False | Video input |
| `json_mode` | bool | False | Structured output |

### Built-in Provider Specs (builtins.yaml)

| Provider | API Style | Default Model | Vision | Env Key |
|----------|-----------|---------------|--------|---------|
| `anthropic` | anthropic | claude-sonnet-4-20250514 | Yes | `ANTHROPIC_API_KEY` |
| `openai` | openai_compat | gpt-4o | Yes | `OPENAI_API_KEY` |
| `ollama` | openai_compat | llama3.2 | llama3.2-vision only | N/A (local) |
| `mistral` | openai_compat | mistral-large-latest | pixtral only | `MISTRAL_API_KEY` |
| `gemini` | google_genai | gemini-2.5-flash | Yes | `GOOGLE_API_KEY` |

### Factory Function

```python
provider = build_agent_provider(
    "openai",
    api_key="sk-...",
    model="gpt-4o-mini",
    vision=True,
)
```

---


## LLMOSToolkit

Auto-generates LangChain tools from daemon module manifests.

### Constructor

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | str | `http://127.0.0.1:40000` | Daemon URL |
| `api_token` | str | None | Bearer token |
| `timeout` | float | 30.0 | HTTP timeout |

### Methods

| Method | Description |
|--------|-------------|
| `get_tools(modules, max_permission)` | Generate LangChain BaseTool list |
| `get_system_prompt(**kwargs)` | Fetch system prompt from daemon (cached) |
| `get_context(**kwargs)` | Fetch full context JSON |
| `execute_parallel(actions, max_concurrent, timeout, group_id)` | Execute via PlanGroup |
| `get_intent_verifier_status()` | Check intent verification status |
| `verify_plan_preview(plan)` | Preview verification result |
| `get_threat_categories()` | List threat categories |
| `refresh()` | Clear cache, reload manifests |
| `close()` | Close HTTP clients |

### LLMOSActionTool

Each tool wraps a single module action:

| Field | Type | Description |
|-------|------|-------------|
| `module_id` | str | e.g. `filesystem` |
| `action_name` | str | e.g. `read_file` |
| `client` | LLMOSClient | Sync HTTP client |
| `async_client` | AsyncLLMOSClient | Shared async client |

**Tool name format**: `{module_id}__{action_name}` (double underscore separator).

**Parameter schema**: Auto-converted from module manifest JSON Schema to Pydantic v2 model via `_json_schema_to_pydantic()`.

---


## HTTP Clients

### LLMOSClient (Synchronous)

| Method | Description |
|--------|-------------|
| `health()` | GET /health |
| `list_modules()` | GET /modules |
| `get_module_manifest(module_id)` | GET /modules/\\{id\\} |
| `submit_plan(plan, async_execution)` | POST /plans |
| `get_plan(plan_id)` | GET /plans/\\{id\\} |
| `get_context(include_schemas, include_examples, max_actions_per_module, format)` | GET /context |
| `get_system_prompt(**kwargs)` | GET /context (format=prompt) |
| `approve_action(plan_id, action_id, decision, reason, modified_params, approved_by)` | POST /plans/\\{id\\}/actions/\\{action_id\\}/approve |
| `get_pending_approvals(plan_id)` | GET /plans/\\{id\\}/pending-approvals |
| `submit_plan_group(plans, group_id, max_concurrent, timeout)` | POST /plan-groups |
| `get_intent_verifier_status()` | GET /intent-verifier/status |
| `verify_plan_preview(plan)` | POST /intent-verifier/verify |
| `get_threat_categories()` | GET /intent-verifier/categories |
| `register_threat_category(category)` | POST /intent-verifier/categories |
| `remove_threat_category(category_id)` | DELETE /intent-verifier/categories/\\{id\\} |

### AsyncLLMOSClient

Same core interface as `LLMOSClient`, all methods `async`. Uses `httpx.AsyncClient`.

**Constructor** (additional parameters vs `LLMOSClient`):

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `app_id` | str | `"default"` | Default application ID for session endpoints |
| `session_id` | str or None | None | Active session ID — injected as `X-LLMOS-Session` header |

When `session_id` is set, all `submit_plan()` calls automatically include the `X-LLMOS-Session` header.

**Session management methods**:

| Method | Description |
|--------|-------------|
| `create_session(app_id, *, agent_id, expires_in_seconds, idle_timeout_seconds, allowed_modules, permission_grants, permission_denials)` | POST /applications/\\{app_id\\}/sessions |
| `get_session(session_id, app_id)` | GET /applications/\\{app_id\\}/sessions/\\{id\\} |
| `delete_session(session_id, app_id)` | DELETE /applications/\\{app_id\\}/sessions/\\{id\\} |

All session methods use `self._app_id` as the default `app_id` when not specified.

**Manual session usage**:

```python
async with AsyncLLMOSClient(app_id="myapp") as client:
    session = await client.create_session(
        expires_in_seconds=3600,
        allowed_modules=["filesystem"],
    )
    client.session_id = session["session_id"]
    # All subsequent submit_plan() carry X-LLMOS-Session.
    result = await client.submit_plan(plan)
    await client.delete_session(client.session_id)
    client.session_id = None
```

---


## SafeguardConfig

Client-side safety rails for autonomous agents.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `protected_windows` | list[str] | 9 patterns | Regex patterns for windows to never interact with |
| `max_consecutive_failures` | int | 3 | Max failures before abort |
| `dangerous_hotkeys` | list[list[str]] | 2 combos | Blocked key combinations |

**Default protected windows**: VSCode, Code-OSS, Terminal, Konsole, gnome-terminal, xterm, tilix, alacritty, kitty.

**Default dangerous hotkeys**: `Alt+F4`, `Ctrl+Alt+Delete`.

### Methods

| Method | Description |
|--------|-------------|
| `is_hotkey_blocked(keys)` | Returns reason string if blocked, None if safe |
| `validate_plan_steps(steps)` | Returns list of warning strings |

---


## Dependencies

### Core (Required)
- `langchain-core` ^0.2
- `httpx` ^0.27
- `pydantic` ^2.7

### Optional (Provider SDKs)

| Extra | Package | Version |
|-------|---------|---------|
| `anthropic` | anthropic | >=0.25 |
| `openai` | openai | >=1.0 |
| `gemini` | google-generativeai | >=0.7 |
| `yaml` | pyyaml | ^6.0 |
| `all` | All of the above | — |

Install: `pip install langchain-llmos[all]`

---


## Usage Examples

### Multi-Provider Agent

```python
from langchain_llmos import ComputerUseAgent

# Anthropic Claude
agent = ComputerUseAgent(provider="anthropic", api_key="sk-ant-...")

# OpenAI GPT-4o
agent = ComputerUseAgent(provider="openai", api_key="sk-...")

# Ollama (local, free)
agent = ComputerUseAgent(provider="ollama", model="llama3.2")

# Gemini
agent = ComputerUseAgent(provider="gemini")

# Mistral
agent = ComputerUseAgent(provider="mistral", api_key="...")
```

### LangChain Integration

```python
from langchain_llmos import LLMOSToolkit
from langchain.agents import AgentExecutor, create_tool_calling_agent

toolkit = LLMOSToolkit()
tools = toolkit.get_tools(modules=["filesystem", "os_exec"])
system_prompt = toolkit.get_system_prompt()

agent = create_tool_calling_agent(llm, tools, prompt)
executor = AgentExecutor(agent=agent, tools=tools)
result = executor.invoke({"input": "List files in home directory"})
```

### Custom Provider

```python
from langchain_llmos.providers import ProviderRegistry, ProviderSpec

registry = ProviderRegistry()
registry.load_builtins()

# Register custom OpenAI-compatible provider
registry.register(ProviderSpec(
    provider_id="my_provider",
    api_style="openai_compat",
    base_url="https://my-llm.example.com/v1",
    env_key="MY_PROVIDER_KEY",
    default_model="my-model-v1",
))

provider = registry.build("my_provider")
```

### Parallel Execution

```python
toolkit = LLMOSToolkit()
result = toolkit.execute_parallel([
    {"module": "filesystem", "action": "read_file", "params": {"path": "/etc/hostname"}},
    {"module": "os_exec", "action": "run_command", "params": {"command": ["uname", "-a"]}},
])
```
