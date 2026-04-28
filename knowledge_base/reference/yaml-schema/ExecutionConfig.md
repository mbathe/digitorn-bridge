---
id: yaml-schema-executionconfig
title: "ExecutionConfig - YAML schema reference"
type: schema-reference
model: ExecutionConfig
is_root: false
keywords: [executionconfig, context, credentials_schema, default_channel, direct_modules, entry_agent, greeting, hooks, input, max_concurrent_activations, max_sessions_per_user]
---

# ExecutionConfig

## Description
Execution mode and runtime parameters.

Example::

execution:
mode: one_shot
entry_agent: coordinator
max_turns: 50
timeout: 300
input:
type: text
required: true
output:
type: json

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `mode` | 'one_shot' \| 'conversation' \| 'background' \| 'pipeline' |  | `'one_shot'` | Execution mode: 'one_shot', 'conversation', 'background', or 'pipeline'. |
| `entry_agent` | str |  | `''` | Agent to start with. Default: first agent in list. |
| `max_turns` | int |  | `50` | Maximum agent loop iterations (per turn for conversation, per activation for background). |
| `timeout` | float |  | `300.0` | Timeout in seconds (per turn for conversation, per activation for background). |
| `input` | [InputConfig](InputConfig.md) |  | `InputConfig(type='text', accept=[], max_size='', description='', required=True)` | Input contract (one_shot mode). |
| `output` | [OutputConfig](OutputConfig.md) |  | `OutputConfig(type='text', format='', description='', schema_def={})` | Output contract (one_shot mode). |
| `greeting` | str |  | `''` | Welcome message displayed at conversation start. |
| `triggers` | list[[TriggerConfig](TriggerConfig.md)] |  | `[]` | Triggers for background mode. |
| `session_mode` | 'mono' \| 'multi' |  | `'mono'` | Background session mode: 'mono' (1 session per user, auto-created) or 'multi' (N sessions per user, created via API with custom params). |
| `max_sessions_per_user` | int |  | `10` | Max background sessions per user in multi mode (0 = unlimited). Ignored in mono mode. |
| `max_concurrent_activations` | int |  | `20` | Max concurrent LLM calls when a broadcast trigger fires. Prevents rate limit storms when thousands of sessions exist. Activations beyond this limit are queued and processed in order. |
| `credentials_schema` | [CredentialsSchemaConfig](CredentialsSchemaConfig.md) \| null |  | `None` | Optional declarative credentials schema. Declares every external service (OpenAI API, Notion OAuth, Slack bot, Postgres DB, MCP servers, …) the app needs to run. The daemon exposes this to the Flutter client which renders a typed form, and blocks activations until all required providers are filled for the current user. |
| `payload_schema` | [PayloadSchemaConfig](PayloadSchemaConfig.md) \| null |  | `None` | Optional declarative schema for the per-session user payload (prompt + typed metadata + file slots). When set, the Flutter dashboard renders a typed form and the daemon validates the payload before firing triggers. Only meaningful in ``mode: background``. |
| `workspace` | str |  | `''` | Working directory for the app. Defaults to the current directory. Auto-indexed at startup for faster file search. Supports {{variables}} and {{env.PWD}}. |
| `workspace_mode` | 'none' \| 'required' \| 'fixed' \| 'auto' |  | `'auto'` | How workspace is handled: 'none' = no workspace (chatbot, Q&A). 'required' = user must select a workspace before chatting. 'fixed' = use the workspace path from YAML, no override allowed. 'auto' = use YAML workspace if set, allow override per session. |
| `sandbox` | [SandboxConfig](SandboxConfig.md) \| null |  | `None` | OS-level sandbox configuration for per-session isolation. When set, workers run in kernel-enforced sandboxes with Landlock, seccomp, namespaces, and process hardening. Use 'level' presets for quick configuration, or fine-tune individual settings. |
| `project_memory` | str |  | `'auto'` | Path to a project memory file loaded into the system prompt at startup. Set to 'auto' to scan for .digitorn.md, CLAUDE.md, or README.md in the workspace. Set to a specific path (relative to workspace) to load that file. Set to '' (empty) to disable. |
| `direct_modules` | list[str] |  | `[]` | Module IDs whose actions are always injected as direct tools, even when the system uses discovery mode for other modules. Use this for fundamental operations the agent should never need to 'discover'. Example: ['filesystem', 'git'] ensures read/edit/status are always one call away. |
| `tool_injection` | 'direct' \| 'compact_direct' \| 'discovery' \| null |  | `None` | Force a specific tool injection mode: 'direct', 'compact_direct', or 'discovery'. If not set, the mode is auto-detected based on tool count vs context window. Use 'discovery' to keep the prompt small with many modules. |
| `context` | [ContextConfig](ContextConfig.md) |  | `ContextConfig(max_tokens=0, output_reserved=4096, strategy='summarize', keep_recent=10, compression_trigger=0.75, summary_max_tokens=1024, auto_compact=True, summary_brain=None)` | Context window management configuration. |
| `hooks` | list[[HookConfig](HookConfig.md)] |  | `[]` | Internal hooks evaluated during the agent loop. Each hook has a condition and an action. Works in all execution modes. |
| `watchers` | bool |  | `False` | Enable persistent watcher capabilities. When true, the agent can start periodic watchers to monitor data sources (APIs, files, databases, processes) and get notified only when something interesting happens. Uses smart escalation to minimize token usage. |
| `scheduler` | bool |  | `False` | Enable scheduler capabilities. When true, the agent can schedule one-shot timers, cron jobs, and reminders. Jobs persist across daemon restarts. Requires watchers to also be enabled. |
| `default_channel` | str |  | `'llm_notification'` | Default output channel for scheduled jobs and watchers. References a channel instance name from the 'channels:' block, or 'llm_notification' (always available). Can be overridden per-job via output_channel. |

## Linked models
- [ContextConfig](ContextConfig.md)
- [CredentialsSchemaConfig](CredentialsSchemaConfig.md)
- [HookConfig](HookConfig.md)
- [InputConfig](InputConfig.md)
- [OutputConfig](OutputConfig.md)
- [PayloadSchemaConfig](PayloadSchemaConfig.md)
- [SandboxConfig](SandboxConfig.md)
- [TriggerConfig](TriggerConfig.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
