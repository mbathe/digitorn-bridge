---
id: yaml-schema-appdefinition
title: "AppDefinition - YAML schema reference"
type: schema-reference
model: AppDefinition
is_root: true
keywords: [appdefinition, app-definition, yaml-root, agents, app, behavior, capabilities, channels, execution, features, middleware, modules, pipeline]
---

# AppDefinition

**This is the root block** - top-level in `app.yaml`.

## Description
Root model - direct parse target for an app YAML file.

Example YAML::

app:
app_id: my-agent
name: "My Agent"

variables:
workspace: "{{env.PWD}}"

modules:
database:
setup:
- action: connect
params:
connection_id: main
driver: sqlite
database: "{{workspace}}/data.db"
constraints:
allowed_actions: [fetch_results, list_tables]
blocked_actions: [execute_query]

agents:
- id: coordinator
role: coordinator
brain:
provider: deepseek
model: deepseek-chat
temperature: 0.2
config:
api_key: "{{secret.DEEPSEEK_API_KEY}}"
base_url: "https://api.deepseek.com/v1"
system_prompt: "You are a coordinator."

capabilities:
default_policy: auto
max_risk_level: medium
grant:
- module: database
actions: [fetch_results]
deny:
- module: database
actions: [execute_query]
reason: "Read-only mode"

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `app` | [AppMeta](AppMeta.md) | ✓ | - | Application identity. |
| `variables` | dict[str, str] |  | `{}` | Template variables available as {{name}} in params and constraints. |
| `modules` | dict[str, [ModuleBlock](ModuleBlock.md)] |  | `{}` | Per-module configuration. Keys are module IDs. |
| `agents` | list[[AgentDefinition](AgentDefinition.md)] |  | `[]` | Agent definitions. Each agent has a brain (LLM config) and role. |
| `execution` | [ExecutionConfig](ExecutionConfig.md) |  | `ExecutionConfig(mode='one_shot', entry_agent='', max_turns=50, timeout=300.0, input=InputConfig(type='text', accept=[], max_size='', description='', required=True), output=OutputConfig(type='text', format='', description='', schema_def={}), greeting='', triggers=[], session_mode='mono', max_sessions_per_user=10, max_concurrent_activations=20, credentials_schema=None, payload_schema=None, workspace='', workspace_mode='auto', sandbox=None, project_memory='auto', direct_modules=[], tool_injection=None, context=ContextConfig(max_tokens=0, output_reserved=4096, strategy='summarize', keep_recent=10, compression_trigger=0.75, summary_max_tokens=1024, auto_compact=True, summary_brain=None), hooks=[], watchers=False, scheduler=False, default_channel='llm_notification')` | Execution mode and runtime parameters. |
| `capabilities` | [CapabilitiesConfig](CapabilitiesConfig.md) \| null |  | `None` | Application security capabilities (grant/deny). When absent, no security enforcement is applied (dev/test mode). |
| `behavior` | [BehaviorConfig](BehaviorConfig.md) \| null |  | `None` | Behavioral enforcement rules. Actively monitored at runtime - violations are detected and signaled to the agent immediately. Use a preset profile (coding, research, data, creative, assistant) or define custom rules. |
| `channels` | dict[str, [ChannelInstanceConfig](ChannelInstanceConfig.md)] |  | `{}` | Named output channel instances. Keys are instance names (e.g. 'slack_alerts', 'email_reports'). Used by scheduler and watchers to route notifications to external systems. |
| `workspace` | [WorkspaceBlock](WorkspaceBlock.md) \| null |  | `None` | Workspace config - tells the client this app uses a virtual file workspace streamed via Socket.IO. The agent writes files with WsWrite/WsEdit/WsDelete and the client renders them based on render_mode (react, html, markdown, slides, code). |
| `preview` | [PreviewConfig](PreviewConfig.md) \| null |  | `None` | Optional dev-server preview for apps shipping a web UI (Vite, Next, etc.). The daemon spawns the command on deploy and exposes it via /api/apps/{app_id}/preview/dev/*. |
| `widgets` | [WidgetsConfig](WidgetsConfig.md) \| null |  | `None` | Declarative UI widgets rendered by the Flutter client. The compiler validates the tree at deploy time; the agent can push live widget render/update events to per-session zones (inline, chat_side, workspace_tabs, modals). |
| `middleware` | list[dict[str, any]] |  | `[]` | App-level middleware pipeline. Runs before/after each LLM call. Built-in: mask_secrets, prompt_inject, content_filter, rag_inject, response_filter. Custom: {custom: {path: './my_mw.py', class: 'MyMiddleware'}} |
| `skills` | list[dict[str, str]] |  | `[]` | App-level skills - reusable command files (.md) the agent can invoke. Each entry: {command: '/name', description: '...', path: './skills/name.md'} |
| `pipeline` | list[[PipelineStep](PipelineStep.md)] |  | `[]` | Pipeline of apps to execute in sequence (one_shot mode only). Each step calls a deployed app and passes its output to the next step. Steps: [{app: 'app_id', input: '{{input}}'}, {app: 'other', input: '{{steps[0].output}}'}] |
| `features` | dict[str, bool] |  | `{}` | UI feature toggles consumed by the Flutter client. Keys: voice, attachments, tools_panel, snippets, tasks_panel, memory_panel, context_ring, markdown, slash_commands, message_actions, status_pills, token_badges. Missing keys default to true (feature visible). Also accepted nested under app.features for client compat. |
| `theme` | dict[str, str] |  | `{}` | Client theme override map. Keys: accent (hex like '#6EE7B7' - overrides app.color), background (hex, client-reserved). |
| `slash_commands` | list[dict[str, str]] |  | `[]` | Custom /slash palette entries rendered by the client. Each entry: {command: 'deploy', description: '…', template: 'Deploy to {env}'}. Currently parsed only; the Flutter client surfaces them in a later release. |

## Linked models
- [AgentDefinition](AgentDefinition.md)
- [AppMeta](AppMeta.md)
- [BehaviorConfig](BehaviorConfig.md)
- [CapabilitiesConfig](CapabilitiesConfig.md)
- [ChannelInstanceConfig](ChannelInstanceConfig.md)
- [ExecutionConfig](ExecutionConfig.md)
- [ModuleBlock](ModuleBlock.md)
- [PipelineStep](PipelineStep.md)
- [PreviewConfig](PreviewConfig.md)
- [WidgetsConfig](WidgetsConfig.md)
- [WorkspaceBlock](WorkspaceBlock.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
