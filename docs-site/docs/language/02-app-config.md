---
id: app-config
---

# App Configuration

The canonical reference for the Digitorn app YAML. Every field on this
page maps to a Pydantic field in
 (or )
and is enforced at compile time with `extra: forbid` - unknown keys
are rejected.

## YAML structure (v2)

A canonical Digitorn app declares **eight top-level blocks** plus an
optional ``schema_version`` (`schema.py` `AppDefinition`):

```yaml
schema_version: 2  # optional, default 2 (forward-compat declaration)

app:        # Identity. Required.
runtime:    # Lifecycle: mode, triggers, hooks, middleware, pipeline,
            # context, max_turns, timeout, workdir, ...
agents:     # List of agent definitions.
tools:      # What the agent can call: modules, capabilities, channels.
security:   # Runtime boundaries: behavior, sandbox, credentials_schema.
ui:         # Pure display: theme, features, widgets, workspace renderer,
            # preview, slash_commands, quick_prompts, greeting.
dev:        # Developer affordances: skills, variables, include.
flow:       # Optional - declarative orchestration graph. Top-level
            # in v2 because the model is different from agent-driven
            # coordination: explicit nodes and edges, not Agent() calls.
```

Only `app:` is strictly required. The other seven default to empty (or
to a default-instance model) - but a useful app declares at least
`agents:` and a couple of modules under `tools:`.

> **Migrating from the legacy flat shape?** Run
> `digitorn yaml migrate-v2 path/to/app.yaml` (CLI command at
> ). The compiler keeps accepting
> legacy YAMLs (`execution:`, `modules:` at the top level, ...) by
> reshaping them via before validation.
> See [the index migration table](/docs/language/#migration-from-the-legacy-flat-shape).

## `app:` - Identity

`schema.py` `AppMeta` (`extra: forbid`).

```yaml
app:
  app_id: my-app                      # Required
  name: "My Application"              # Required
  version: "1.0"                      # default "1.0"
  schema_version: "1"                 # default "1"
  description: "What this app does"   # default ""
  author: "your-name"                 # default ""
  tags: [coding, assistant]           # default []
  icon: "🤖"                          # emoji / icon-name / URL / data URI
  color: "#8B5CF6"                    # hex; auto-generated if empty
  category: "coding"                  # default "general"
  quick_prompts:                      # one-click suggestions
    - label: "New PR"
      message: "Open a PR with the latest changes"
      icon: "🚀"
```

| Field | Type | Default |
|-------|------|---------|
| `app_id` | string | *required* |
| `name` | string | *required* |
| `version` | string | `"1.0"` |
| `schema_version` | string | `"1"` |
| `description` | string | `""` |
| `author` | string | `""` |
| `tags` | list[string] | `[]` |
| `icon` | string | `""` |
| `color` | string | `""` |
| `category` | string | `"general"` |
| `quick_prompts` | list[QuickPrompt] | `[]` |

`QuickPrompt` (`typed_models.py`, `extra: allow`) is `{label*, message*, icon}` - `label` and `message` are required strings, `icon` defaults to `""`.

> **Scope note**. Apps deploy under a `(app_id, scope, owner_user_id)`
> triple. The YAML carries no scope field - the deploy endpoint picks
> one (`scope=system` by default, `scope=user` from the JWT for
> private installs). See [Multi-Tenant Installs](45-multi-tenant.md).

> **Mirrors**. `app.features` and `app.theme` exist on the schema but
> are **deprecated** at this nested level - the canonical home is
> `ui.features` and `ui.theme`. The compiler lifts them with the
> alias pass; the migrator strips them.

## `runtime:` - Lifecycle and execution policy

`schema.py` `RuntimeBlock` (`extra: forbid`). Every field that
controls per-turn daemon behavior lives here.

```yaml
runtime:
  mode: conversation
  entry_agent: coordinator
  max_turns: 50
  timeout: 300.0
  session_mode: mono
  max_sessions_per_user: 10
  max_concurrent_activations: 20
  workdir: '{{env.PWD}}'
  workdir_mode: auto
  default_channel: llm_notification
  watchers: false
  scheduler: false
  project_memory: auto
  direct_modules:
  - filesystem
  tool_injection: null
  context:
    '...': null
  triggers:
  - '...'
  hooks:
  - '...'
  middleware:
  - '...'
  pipeline:
  - '...'
  input:
    '...': null
  output:
    '...': null
  payload_schema:
    '...': null
flow:
  '...': null
```

| Field | Type | Default |
|-------|------|---------|
| `mode` | `one_shot | conversation | background | pipeline` | `conversation` |
| `entry_agent` | string | `""` (= first agent in list) |
| `max_turns` | int ≥1 | `50` |
| `timeout` | float >0 | `300.0` |
| `session_mode` | `mono | multi` | `mono` |
| `max_sessions_per_user` | int ≥0 | `10` |
| `max_concurrent_activations` | int ≥1 | `20` |
| `workdir` | string | `""` |
| `workdir_mode` | `none | required | fixed | auto` | `auto` |
| `project_memory` | string | `"auto"` |
| `direct_modules` | list[string] | `[]` |
| `tool_injection` | `direct | compact_direct | discovery | None` | `None` |
| `context` | ContextConfig | default-instance |
| `hooks` | list[HookConfig] | `[]` |
| `watchers` | bool | `false` |
| `scheduler` | bool | `false` |
| `default_channel` | string | `"llm_notification"` |
| `middleware` | list[dict] | `[]` |
| `pipeline` | list[PipelineStep] | `[]` |
| `triggers` | list[TriggerConfig] | `[]` |
| `input`, `output` | InputConfig, OutputConfig | default-instances |
| `payload_schema` | PayloadSchemaConfig\|None | `null` |

### `runtime.context` - Context window management

`schema.py` `ContextConfig` (`extra: forbid`). Eight fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_tokens` | int [0, 2_000_000] | `0` | `schema.py`. `0` = auto-detect from provider. |
| `output_reserved` | int | `4096` | `schema.py`. Reserved for output generation when computing pressure. |
| `strategy` | `truncate | summarize` | `summarize` | `schema.py` |
| `keep_recent` | int | `10` | `schema.py`. Most-recent messages preserved verbatim during compaction. |
| `compression_trigger` | float [0, 1] | `0.75` | `schema.py`. Pressure ratio that triggers auto-compaction. |
| `summary_max_tokens` | int | `1024` | `schema.py` |
| `auto_compact` | bool | `true` | `schema.py`. Auto-injects a `context_pressure` hook if none declared. |
| `summary_brain` | AgentBrain\|None | `null` | `schema.py`. Use a cheap/fast model for summaries instead of the agent's main brain. |

Per-agent override: each agent can re-declare `brain.context` with the
same fields.

## `agents:` - Agent definitions

`schema.py` `AgentDefinition` (list-shape, `extra: forbid`). Full
field reference is on the [Agents](03-agents.md) page; here is the
shape and how it nests in the app:

```yaml
agents:
  - id: coordinator                 # Required, slug
    role: coordinator               # coordinator | specialist | worker | supervisor (see Agents doc)
    brain:                          # Required - see Agents doc for AgentBrain fields
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You are the coordinator.
    plan_first: true
    delegate_to: [explorer, writer]
    pool:                           # AgentPoolConfig - see Multi-Agent doc
      max_workers: 3
    modules:                        # per-agent module restriction
      - filesystem
      - { shell: [bash] }           # only the bash action on shell
    hooks: []                       # agent-scoped hooks
```

See [Agents](03-agents.md) for the brain (provider/model/temperature/
fallback/context/credential), pool, delegate_to, and per-agent module
restriction. See [Multi-Agent](12-multi-agent.md) for coordination
patterns.

## `tools:` - Modules, capabilities, channels

`schema.py` `ToolsBlock` (`extra: forbid`).

```yaml
tools:
  modules:                          # dict[str, ModuleBlock] - keys are module ids
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
  capabilities:
    default_policy: auto             # auto | approve | block (default: approve)
    max_risk_level: medium           # low | medium | high
    grant: [{ module: filesystem, actions: [read, write] }]
    approve: [{ module: shell, actions: [bash] }]
    deny: [{ module: shell, actions: [kill] }]
    approval_timeout: 300            # seconds, [30, 3600]
    hidden_modules: []               # ids hidden from agent index
    hidden_actions: []               # specific actions hidden
  channels:                          # dict[str, ChannelInstanceConfig] - see Channels doc
    slack_alerts:
      type: slack
      config: { ... }
```

### `tools.modules` - Module configuration

`schema.py`. Map of module-id → `ModuleBlock` (`schema.py`).
Each `ModuleBlock` has 5 fields (`extra: forbid`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `config` | dict | `{}` | `schema.py`. Static config pushed via `module.on_config_update(config)` at bootstrap. Validated against the module's `CONFIG_MODEL` if declared. |
| `setup` | list[SetupStep] | `[]` | `schema.py`. Ordered actions executed at bootstrap. Each step = `{action: str, params: dict}`. |
| `constraints` | dict | `{}` | `schema.py`. Universal: `allowed_actions`, `blocked_actions`. Module-specific keys validated against the module's `ConstraintSpec`. |
| `middleware` | list[dict] | `[]` | `schema.py`. Module-level middleware pipeline. Example: `[{audit: {log_params: true}}, {retry: {max_attempts: 3}}]`. |
| `credential` | string \| dict \| null | `null` | `schema.py`. Compact: `credential: openai_main`. Explicit: `credential: { ref: openai_main, scope: per_user }`. Resolved at activation time. |

`SetupStep` (`schema.py`):
- `action: str` (required) - action name on the module
- `params: dict` (default `{}`) - may contain `{{variables}}`

The 22 modules shipped by the daemon are listed in
[the index](/docs/language/#modules). Per-module reference docs live
under [modules/reference/](../reference/modules/). `context_builder`
and `llm_provider` are auto-loaded - never declare them.

### `tools.capabilities` - Grant / approve / deny

`schema.py` `CapabilitiesConfig` (`extra: forbid`). Optional
(`null` = dev/test mode, no enforcement). When present:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_policy` | `auto | approve | block` | `approve` | `schema.py` |
| `max_risk_level` | `low | medium | high` | `medium` | `schema.py` |
| `grant` | list[CapabilityGrant] | `[]` | Explicit allows |
| `approve` | list[CapabilityGrant] | `[]` | Each call pauses for user approval |
| `deny` | list[CapabilityGrant] | `[]` | Hard block |
| `approval_timeout` | int [30, 3600] | `300` | Seconds before auto-deny |
| `hidden_modules` | list[string] | `[]` | Modules hidden from the agent index but still callable from setup steps / hooks / channels |
| `hidden_actions` | list[CapabilityGrant] | `[]` | Specific actions hidden but executable internally |

`CapabilityGrant` (`schema.py`) is `{module: str, actions: list[str], reason: str}`. Empty `actions` = all actions on the module.

See [Security](11-security.md) for the resolution algorithm and
risk-level classification.

### `tools.channels` - Output channel instances

`schema.py`. Map of channel-instance-name → `ChannelInstanceConfig`
(`schema.py`). See [Channels (Bidirectional I/O)](40-channels.md)
for the full surface.

## `security:` - Runtime boundaries

`schema.py` `SecurityBlock` (`extra: forbid`). All three sub-fields
are optional.

```yaml
security:
  behavior:                          # see Behavior Engine doc
    profile: coding
    classify_turns: true
  sandbox:                           # see OS Sandbox doc
    level: strict
  credentials_schema:                # declarative external secrets
    providers: { ... }
```

| Field | Type | Source | Doc |
|-------|------|--------|-----|
| `behavior` | BehaviorConfig\|None | `schema.py` | [Behavior Engine](43-behavior.md) |
| `sandbox` | SandboxConfig\|None | `schema.py` | [OS Sandbox](35-sandbox.md) |
| `credentials_schema` | CredentialsSchemaConfig\|None | `schema.py` | [credentials.md](../reference/runtime/credentials.md) |

## `ui:` - Display layer (daemon never reads)

`schema.py` `UIBlock` (`extra: forbid`). Pure client-side rendering -
every field here is consumed by the Flutter / web client, not by the
daemon.

The block ships **two layers**:

1. **Legacy** (kept for backward compatibility): `theme`, `features`,
   `widgets`, `workspace.render_mode`, `slash_commands`,
   `quick_prompts`, `greeting`.
2. **Chat layout / behaviour** (added 2026-05-04): `layout`,
   `density`, `thinking`, `tool_calls`, `composer`, `visual`, plus
   the extended `workspace` fields `position`, `width_pct`,
   `auto_open_on_first_tool`.

Every new sub-block is **optional**; omitting it preserves the
historical client behaviour.

```yaml
ui:
  # ── Theme & visual (open dict) ───────────────────────────────
  theme:
    accent: "#3b82f6"                 # hex, used by the client
    background: "#0f1115"             # hex, reserved for the client

  # ── Feature toggles (12 booleans, default = true) ─────────────
  features:
    voice: true
    attachments: true
    tools_panel: true
    snippets: true
    tasks_panel: true
    memory_panel: true
    context_ring: true
    markdown: true
    slash_commands: true
    message_actions: true
    status_pills: true
    token_badges: true

  # ── Workspace pane (renderer + layout) ────────────────────────
  workspace:
    render_mode: react                # react|html|markdown|slides|code|latex|builder|auto
    entry_file: src/App.tsx
    title: My App
    position: right                   # right|bottom|hidden|overlay
    width_pct: 50                     # 10..90 split ratio
    auto_open_on_first_tool: false    # Lovable-style auto-open

  # ── Declarative UI widgets (v1) ───────────────────────────────
  widgets:
    version: 1
    nodes: [...]                      # see Widgets doc

  # ── Slash commands palette ────────────────────────────────────
  slash_commands:
    - command: /deploy
      description: Deploy the current app
      template: "Deploy {{branch}} to prod"

  # ── Quick prompts (composer chips) ────────────────────────────
  quick_prompts:
    - label: Identify model
      icon: 🔍
      message: "Which model are you?"

  # ── Empty-state welcome message ───────────────────────────────
  greeting: |
    Hello! Ask me anything.

  # ── Chat layout / behaviour (optional) ────────────────────────
  layout: default                     # default|code|builder|research|minimal|lovable
  density: comfortable                # compact|comfortable

  thinking:
    visible: true                     # hide thinking blocks entirely when false
    collapsed_default: true           # initial collapsed state

  tool_calls:
    collapsed_default: true           # tool chips collapsed on first render
    show_silent: false                # show plumbing tools (memory, agent_spawn, …)

  composer:
    file_upload: true                 # paperclip / drag-drop attachment
    voice: false                      # mic button (default OFF, opt-in)
    slash_commands: true              # `/`-palette
    quick_prompts_visible: true       # chips above the composer when empty

  visual:
    accent: "#3b82f6"                 # fallback chain: visual.accent → theme.accent → app.color
    bubble_style: card                # card|flat|minimal
    user_bubble_alignment: right      # right (default) | left
```

### Legacy fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `theme` | `dict[str, str]` | `{}` | Open dict. Keys: `accent` (hex), `background` (hex). Custom keys passed through untouched. |
| `features` | `dict[str, bool]` | `{}` | 12 known toggles + any custom key. Missing keys default to `true`. See [Client Manifest → features](44-client-manifest.md#uifeatures---12-toggles). |
| `widgets` | `WidgetsConfig \| null` | `null` | See [Widgets](42-widgets.md). |
| `slash_commands` | `list[SlashCommand]` | `[]` | `/`-palette entries. |
| `quick_prompts` | `list[QuickPrompt]` | `[]` | Mirror of `app.quick_prompts`; the client merges both. |
| `greeting` | `str` | `""` | Empty-state welcome message. |

### Workspace block (`UIBlock.workspace`, `extra: forbid`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `render_mode` | `str` | `"auto"` | `react` \| `html` \| `markdown` \| `slides` \| `code` \| `latex` \| `builder` \| `auto`. Auto-detects from the first file. |
| `entry_file` | `str \| null` | `null` | Default file the renderer opens. |
| `title` | `str \| null` | `null` | Workspace toolbar label. |
| `position` | `str` | `"right"` | `right` \| `bottom` \| `hidden` \| `overlay`. Where the pane sits relative to chat. |
| `width_pct` | `int (10..90)` | `50` | Workspace width as a percentage of the chat-vs-workspace split. Ignored when `position` is `hidden` / `overlay`. |
| `auto_open_on_first_tool` | `bool` | `false` | When `true`, the client opens the workspace pane the first time the agent writes a file (Lovable-style). |

### Chat layout / behaviour blocks (optional, added 2026-05-04)

All sub-blocks below are `extra: forbid` Pydantic models. Omit any
of them to keep the client's historical defaults.

#### `ui.layout`

`str`, default `"default"`. Allowed: `default`, `code`, `builder`,
`research`, `minimal`, `lovable`.

High-level preset that the client uses to pre-fill any sub-block the
YAML did NOT define. Fine-grained sub-blocks ALWAYS win over the
preset, so a YAML can derive from `lovable` and tweak just one knob.

#### `ui.density`

`str`, default `"comfortable"`. Allowed: `compact`, `comfortable`.
Controls bubble spacing.

#### `ui.thinking`

- `visible: bool` (default `true`) - when `false`, thinking blocks
  are hidden entirely.
- `collapsed_default: bool` (default `true`) - initial collapsed
  state of thinking blocks.

#### `ui.tool_calls`

- `collapsed_default: bool` (default `true`) - initial collapsed
  state of tool-call chips.
- `show_silent: bool` (default `false`) - when `true`, plumbing
  tools (memory ops, `agent_spawn` internals, discovery
  meta-tools) are rendered.

#### `ui.composer`

Mirrors the legacy `ui.features` flags for the same concepts. When
both are present, the typed `composer.X` wins.

- `file_upload: bool` (default `true`) - paperclip / drag-drop
  attachment.
- `voice: bool` (default `false`) - microphone button (opt-in).
- `slash_commands: bool` (default `true`) - `/`-palette popup.
- `quick_prompts_visible: bool` (default `true`) - suggested-prompt
  chips above the composer when the conversation is empty.

#### `ui.visual`

- `accent: str` (hex, default `""`) - hex accent colour. Fallback
  chain: `visual.accent` → `theme.accent` → `app.color`.
- `bubble_style: str` (default `"card"`) - `card`, `flat`, or
  `minimal`.
- `user_bubble_alignment: str` (default `"right"`) - `right` or
  `left`.

#### `ui.activity`

`ActivityPanelBlock` (`schema.py`, `extra: forbid`). Opt-in pane
that surfaces live sub-agent fan-out, background tasks, and recent
terminal events. **Omit the block to hide the entry entirely** -
simple chat apps stay clean. Apps that orchestrate multi-agent work
opt in - `enabled: bool` (default `true`) - master switch.
- `position: str` (default `"right"`) - `right`, `bottom`, or
  `overlay`.
- `title: str | null` (default `null`) - panel header label.
- `show_running: bool` (default `true`) - live sub-agent strip.
- `show_recent: bool` (default `true`) - recent-terminal-events list.
- `show_stats: bool` (default `true`) - aggregate stats footer
  (success rate, avg duration; pulls from `digitorn_agent_*`
  Prometheus counters).
- `show_bg_tasks: bool` (default `true`) - interleave background
  shell tasks alongside sub-agents.
- `max_recent: int` (default `50`, range `5..500`) - cap on the
  recent-events list (FIFO eviction).
- `auto_open_on_spawn: bool` (default `false`) - auto-switch to
  the Activity pane on first sub-agent spawn.

Driven by the daemon-resource protocol (snapshot + heartbeat +
`turn_terminal` consolidated event). Survives daemon restarts and
socket drops without zombie state. Full reference:
[Client Manifest → ui.activity](44-client-manifest.md#uiactivity---opt-in-sub-agent-observability-pane-2026-05-06).

### Custom typed models

`SlashCommand` (`typed_models.py`, `extra: allow`):

- `command: str` (required) - the `/foo` id
- `description: str` (default `""`)
- `template: str` (default `""`) - message template with `{{var}}`
  placeholders

`QuickPrompt` (`typed_models.py`, `extra: allow`):

- `label: str` (required, min 1) - short button label
- `message: str` (required, min 1) - full prompt sent on click
- `icon: str` (default `""`) - emoji or icon name

## `dev:` - Developer affordances

`schema.py` `DevBlock` (`extra: forbid`).

```yaml
dev:
  skills:                            # /command markdown files
    - command: /commit
      description: Stage + commit + push the current diff
      path: skills/commit.md
  variables:                         # template substitutions
    workspace: "{{env.PWD}}"
    max_lines: "500"
  include:                           # fragmentation
    agents: ./agents/
    hooks: [./hooks/auto-lint.yaml, ./hooks/auto-test.yaml]
```

### `dev.skills`

List of `SkillEntry` (`typed_models.py`, `extra: forbid`):
- `command: str` (required, min length 1) - slash command id
- `description: str` (default `""`) - one-line catalog entry
- `path: str` (required, min length 1) - path to the `.md` file
  relative to the bundle dir

The compiler reads the file at compile time and surfaces it via the
slash-command palette. See [Skills System](21-skills.md).

### `dev.variables`

`dict[str, str]`. Template substitutions exposed as `{{name}}` in
every other field of the YAML. Variables can reference each other
(max recursion depth 10, cycles detected). See **Variables** below.

### `dev.include` - Fragmentation

`IncludeBlock` (`typed_models.py`, `extra: forbid`). Splits
list-shaped sections (`agents`, hooks) into separate files. The
compiler resolves these BEFORE Pydantic validation.

```yaml
dev:
  include:
    agents: ./agents/                                     # directory of YAMLs
    hooks: [./hooks/lint.yaml, ./hooks/auto-test.yaml]    # explicit list
```

Convention: `./agents/*.yaml` and `./hooks/*.yaml` are auto-loaded
even without an explicit `include:` entry.

## `flow:` - Declarative orchestration graph (8th block)

`schema.py` `AppDefinition.flow` (FlowConfig | None, default
`null`). Promoted to a **top-level block** in v2 because the model
is different from agent-driven coordination: a directed graph of
nodes with conditional edges, declared up front, instead of
runtime `Agent` tool calls.

`FlowConfig` is defined in (`extra: forbid`).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string (min 1) | yes | Flow identifier, unique within the app (`flow.py`). |
| `entry` | string (min 1) | yes | Starting node id (`flow.py`). |
| `description` | string | no | Free-form summary (`flow.py`). |
| `max_iterations` | int ≥ 0 | conditional | Per-flow cap on total node visits. `0` = no cap, only valid for acyclic flows. Required ≥ 1 when the graph has any cycle (`flow.py`). |
| `nodes` | list[FlowNode] (min 1) | yes | Nodes that compose the graph (`flow.py`). |

`FlowNode.type` is a discriminator with six values: `agent`, `tool`,
`parallel`, `approval`, `decision`, `terminal`. Each node carries
type-specific fields plus optional `routes` (conditional outgoing
edges) and `on_error` handlers.

```yaml
flow:
  id: support_main
  entry: triage
  max_iterations: 100
  nodes:
    - id: triage
      type: agent
      agent: triage
      routes:
        - { when: "category == 'refund'", to: refund }
        - { when: "default", to: end }
    - id: refund
      type: agent
      agent: refund_specialist
      routes:
        - { to: gate }
    - id: gate
      type: approval
      message: "Confirm refund?"
      routes:
        - { when: "approvals.gate == 'approve'", to: end }
        - { when: "default", to: end }
```

> **Backward compatibility.** A YAML that still declares `flow:`
> nested under `runtime:` is accepted by the compiler's alias pass
> (`schema_aliases.py`), which lifts it to top-level before
> validation. The `digitorn yaml migrate-v2` command rewrites it in
> place to the canonical top-level form.

See [Flows](07-flows.md) for the full node-type surface, route
expressions, error handling (`on_error`), the daemon's reachability
and cycle-detection passes, and the runtime semantics
(per-iteration tracing, agent isolation, decision evaluation).

## Variables

The compiler resolves `{{...}}` templates recursively across every
string in the YAML (
`resolve_variables`). Six namespaces, each with a fixed resolution
time.

| Namespace | Syntax | Resolved at |
|-----------|--------|-------------|
| User | `{{my_var}}` | Compile time |
| Environment | `{{env.VAR}}` | Compile time |
| Secret | `{{secret.VAR}}` | Compile time |
| System | `{{sys.VAR}}` | Compile time |
| App | `{{app.FIELD}}` | Compile time |
| Bundle file | `{{prompt.X}}`, `{{skill.X}}`, `{{behavior.X}}`, `{{asset.X}}` | Compile time |
| Runtime | `{{event.X}}`, `{{caller.X}}`, any other dotpath | Run time |

### Fallback operator

```yaml
dev:
  variables:
    timeout: "{{env.TIMEOUT ?? '30'}}"
    region:  "{{env.AWS_REGION ?? 'eu-west-1'}}"
```

If the left side fails to resolve, the right side is used
(`variables.py`). Works with any namespace.

### User variables (`{{my_var}}`)

```yaml
dev:
  variables:
    workspace: "{{env.PWD}}"
    db_name: "{{app.id}}_production"
    max_lines: "500"

agents:
  - id: assistant
    system_prompt: |
      Application: {{app.name}} v{{app.version}}
      Working directory: {{workspace}}
      Max lines: {{max_lines}}
```

### Environment variables (`{{env.VAR}}`)

Read from `os.environ`. **Raises a compilation error if the variable
is not set** (use `??` for optional values).

### Secrets (`{{secret.VAR}}`) - legacy

> **Prefer `credential:` blocks for new apps**
> ([credentials.md](../reference/runtime/credentials.md)). The legacy
> `{{secret.X}}` system still works as a fallback (resolved
> by `runtime_resolver.py`) but new apps should reference
> the centralised credentials vault by name.

Two-step lookup: encrypted per-app database first,
`os.environ` fallback. Stored encrypted at rest with Fernet
(AES-128-CBC + HMAC-SHA256), per app. Manage via the CLI:

```bash
digitorn secret set <app_id> API_KEY "sk-live-abc123"
digitorn secret set <app_id> API_KEY              # prompts (hidden input)
digitorn secret get <app_id> API_KEY
digitorn secret list <app_id>
digitorn secret delete <app_id> API_KEY
```

Or via the daemon's per-app secrets surface (PUT body
`{"value": "..."}`).

The compiler emits a warning when an app uses
`{{secret.X}}` / `{{env.X}}` templates without a
`credential:` block. Run `digitorn yaml migrate-credentials
<file>` to migrate to the credentials vault.

### System variables (`{{sys.*}}`)

Resolved at compile time from `_SYS_VARIABLES`
(`variables.py`). The full list:

| Key | Source | Example |
|-----|--------|---------|
| `sys.timestamp` | `datetime.now(UTC).isoformat` | `2026-05-01T18:30:00+00:00` |
| `sys.date` | `datetime.now(UTC).strftime("%Y-%m-%d")` | `2026-05-01` |
| `sys.time` | `datetime.now(UTC).strftime("%H:%M:%S")` | `18:30:00` |
| `sys.hostname` | `socket.gethostname` | `prod-server-1` |
| `sys.platform` | `sys.platform` | `linux`, `darwin`, `win32` |
| `sys.os` | `platform.system` | `Linux`, `Darwin`, `Windows` |
| `sys.arch` | `platform.machine` | `x86_64`, `arm64` |
| `sys.python_version` | `platform.python_version` | `3.13.12` |
| `sys.cwd` | `os.getcwd` | `/home/user/apps` |
| `sys.user` | `$USER` / `$USERNAME` / `unknown` | `paul` |
| `sys.pid` | `os.getpid` | `12345` |
| `sys.digitorn_version` | package version | `1.0.0` |
| `sys.home` | `~` expansion | `/home/paul` |
| `sys.tmpdir`, `sys.temp_dir` | `tempfile.gettempdir` | `/tmp` |
| `sys.locale` | `$LANG` / `$LC_ALL` / `C` | `en_US.UTF-8` |
| `sys.shell` | detected default shell | `/bin/bash`, `pwsh` |
| `sys.shell_family` | shell category | `bash`, `pwsh`, `cmd` |
| `sys.path_sep` | `os.sep` | `/` or `\` |
| `sys.is_windows` | `"true"` / `"false"` | `"false"` |
| `sys.is_linux` | `"true"` / `"false"` | `"true"` |
| `sys.is_macos` | `"true"` / `"false"` | `"false"` |

Source of truth: `_SYS_VARIABLES` dict in `variables.py`.

### App variables (`{{app.*}}`)

Resolved at compile time from the `app:` block:

| Key | Source field |
|-----|--------------|
| `{{app.id}}` | `app.app_id` |
| `{{app.name}}` | `app.name` |
| `{{app.version}}` | `app.version` |
| `{{app.author}}` | `app.author` |
| `{{app.description}}` | `app.description` |

### Bundle file namespaces

When the bundle directory contains the corresponding folder, these
resolve to file content / URLs at compile time
(`variables.py:_resolve_prompt`, `_resolve_skill`, etc.):

| Pattern | Folder | Resolves to |
|---------|--------|-------------|
| `{{prompt.X}}` | `prompts/X.md` | File content (tries `.md`, `.markdown`, `.txt`, `.prompt`, bare name) |
| `{{skill.X}}` | `skills/X.md` | File content (same fallback chain) |
| `{{behavior.X}}` | `behavior/X.yaml` | Parsed YAML profile, returned as JSON string |
| `{{asset.X}}` | `assets/X.{ext}` | URL `/api/apps/<app_id>/assets/assets/X` (fuzzy-matches `.png`, `.jpg`, `.svg`, ...) |

### Runtime variables (passthrough)

Any `{{dotpath.expr}}` that isn't matched by the namespaces above is
preserved verbatim by the compiler. Modules resolve them at runtime -
typical example is the channels module, which fills `{{event.X}}`
from inbound webhook payloads:

```yaml
tools:
  channels:
    support_inbox:
      type: webhook
      activation:
        prepare:
          - action: database.fetch_results
            params:
              query: "SELECT * FROM clients WHERE phone = '{{event.source}}'"
            as: caller
        context: "Client: {{caller.name}} ({{caller.plan}})"
        message: "{{event.payload.message}}"
```

| Pattern | Resolved by | When |
|---------|-------------|------|
| `{{event.payload.X}}` | channels module | Inbound event arrival |
| `{{event.source}}` | channels module | Sender id (phone, email, IP, ...) |
| `{{caller.X}}` | channels prepare pipeline | After a `prepare` step with `as:` |
| `{{any.dotpath}}` | consuming module | Any unmatched dotpath passes through |

## Migration: legacy → canonical

The compiler's alias pass (`schema_aliases.py`) accepts the legacy
flat shape and reshapes it to canonical before Pydantic validates.
The migration table is in [the index](/docs/language/#migration-from-the-legacy-flat-shape).

To rewrite a YAML in-place to canonical form:

```bash
digitorn yaml migrate-v2 path/to/app.yaml
```

Two cosmetic renames the migrator applies (no compat retention):

- `execution.workspace` → `runtime.workdir`
- `execution.workspace_mode` → `runtime.workdir_mode`

Everything else (lifts to `tools.*`, `security.*`, `ui.*`, `dev.*`,
`runtime.*`) preserves field names.

## Complete example

```yaml
app:
  app_id: invoice-processor
  name: "Invoice Processor"
  version: "3.1"
  author: "Finance Team"
  category: data

runtime:
  mode: conversation
  entry_agent: main
  max_turns: 30
  workdir: "{{env.PWD}}"
  context:
    max_tokens: 200000
    strategy: summarize
    keep_recent: 12

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
      temperature: 0.2
      fallback:
        provider: anthropic
        model: claude-haiku-4-5
        config:
          api_key: "{{secret.ANTHROPIC_API_KEY}}"
    system_prompt: |
      You are {{app.name}} v{{app.version}}.
      Process invoices from {{data_dir}}.

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, write, edit, glob, grep]
    database:
      setup:
        - action: connect
          params:
            connection_id: main
            driver: sqlite
            database: "{{data_dir}}/{{app.id}}.db"
  capabilities:
    default_policy: auto
    deny:
      - { module: shell, actions: [bash] }

security:
  behavior:
    profile: data

ui:
  greeting: "Drop an invoice and I'll extract the line items."
  quick_prompts:
    - label: "Last week"
      message: "Summarize last week's invoices"
      icon: "📊"

dev:
  variables:
    data_dir: "/data/{{app.id}}"
```

## Cross-references

- Per-block deep dives:
  [Agents](03-agents.md), [Tools](04-tools.md),
  [Triggers](09-triggers.md), [Flows](07-flows.md),
  [Middleware](17-middleware.md), [Tool Hooks](31-tool-hooks.md),
  [Context Management](06-context-management.md),
  [Multi-Agent](12-multi-agent.md), [Channels](40-channels.md)
- Security: [Capabilities](11-security.md),
  [Behavior Engine](43-behavior.md),
  [OS Sandbox](35-sandbox.md), [credentials.md](../reference/runtime/credentials.md)
- UI: [Client Manifest](44-client-manifest.md),
  [Widgets](42-widgets.md),
  [Workspace & Preview](41-preview.md)
- Dev: [Skills System](21-skills.md),
  [Bundle namespaces](38-bundle-namespaces.md)

> **Note**. Some content previously in this file (database
> auto-schema injection, business annotations, channel built-in
> types, sandbox detail) covers topics that belong in their dedicated
> reference docs ([modules/reference/database.md](../reference/modules/database.md),
> [40-channels.md](40-channels.md), [35-sandbox.md](35-sandbox.md)).
> Those sections are being relocated in a follow-up pass; this page
> is now strictly the 8-block configuration reference.
