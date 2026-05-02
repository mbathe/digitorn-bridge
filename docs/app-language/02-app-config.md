---
id: app-config
---

# App Configuration

The canonical reference for the Digitorn app YAML. Every field on this
page maps to a Pydantic field in
`packages/digitorn/core/app/schema.py` (or `core/app/typed_models.py`)
and is enforced at compile time with `extra: forbid` — unknown keys
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
            # since v2 because it's a paradigm shift (explicit
            # scenography vs implicit Agent() coordination).
```

Only `app:` is strictly required. The other seven default to empty (or
to a default-instance model) — but a useful app declares at least
`agents:` and a couple of modules under `tools:`.

> **Migrating from the legacy flat shape?** Run
> `digitorn yaml migrate-v2 path/to/app.yaml` (CLI command at
> `core/cli/yaml_migrate.py:44`). The compiler keeps accepting
> legacy YAMLs (`execution:`, `modules:` at the top level, ...) by
> reshaping them via `core/app/schema_aliases.py` before validation.
> See [the index migration table](00-index.md#migration-from-the-legacy-flat-shape).

## `app:` — Identity

`schema.py:35` `AppMeta` (`extra: forbid`).

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

| Field | Type | Default | Source |
|-------|------|---------|--------|
| `app_id` | string | *required* | `schema.py:40` |
| `name` | string | *required* | `schema.py:41` |
| `version` | string | `"1.0"` | `schema.py:42` |
| `schema_version` | string | `"1"` | `schema.py:43` |
| `description` | string | `""` | `schema.py:44` |
| `author` | string | `""` | `schema.py:45` |
| `tags` | list[string] | `[]` | `schema.py:46` |
| `icon` | string | `""` | `schema.py:49`. Emoji, icon name, URL, or `data:` URI. Empty → client generates a colored circle from `app_id`. |
| `color` | string | `""` | `schema.py:57`. Hex format `#8B5CF6`. Empty → auto-derived from `app_id` hash. |
| `category` | string | `"general"` | `schema.py:64`. Used by clients for catalog filtering. Examples: `coding`, `writing`, `research`, `data`, `devops`, `design`, `communication`, `automation`, `general`. |
| `quick_prompts` | list[QuickPrompt] | `[]` | `schema.py:72`. Mirror of `ui.quick_prompts`. |

`QuickPrompt` (`typed_models.py:26`, `extra: allow`) is `{label*, message*, icon}` — `label` and `message` are required strings, `icon` defaults to `""`.

> **Scope note**. Apps deploy under a `(app_id, scope, owner_user_id)`
> triple. The YAML carries no scope field — the deploy endpoint picks
> one (`scope=system` by default, `scope=user` from the JWT for
> private installs). See [Multi-Tenant Installs](45-multi-tenant.md).

> **Mirrors**. `app.features` and `app.theme` exist on the schema but
> are **deprecated** at this nested level — the canonical home is
> `ui.features` and `ui.theme`. The compiler lifts them with the
> alias pass; the migrator strips them.

## `runtime:` — Lifecycle and execution policy

`schema.py:2317` `RuntimeBlock` (`extra: forbid`). Every field that
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

| Field | Type | Default | Source |
|-------|------|---------|--------|
| `mode` | `one_shot | conversation | background | pipeline` | `conversation` | `schema.py:2337` |
| `entry_agent` | string | `""` (= first agent in list) | `schema.py:2341` |
| `max_turns` | int ≥1 | `50` | `schema.py:2345` |
| `timeout` | float >0 | `300.0` | `schema.py:2349` |
| `session_mode` | `mono | multi` | `mono` | `schema.py:2367` |
| `max_sessions_per_user` | int ≥0 | `10` | `schema.py:2375`. `0` = unlimited. |
| `max_concurrent_activations` | int ≥1 | `20` | `schema.py:2379`. Throttle parallel LLM calls when a broadcast trigger fires. |
| `workdir` | string | `""` | `schema.py:2389`. Renamed from legacy `execution.workspace`. Disambiguates from `ui.workspace` (renderer). |
| `workdir_mode` | `none | required | fixed | auto` | `auto` | `schema.py:2398` |
| `project_memory` | string | `"auto"` | `schema.py:2406` |
| `direct_modules` | list[string] | `[]` | `schema.py:2411` |
| `tool_injection` | `direct | compact_direct | discovery | None` | `None` | `schema.py:2415` |
| `context` | ContextConfig | default-instance | `schema.py:2420` — see below |
| `hooks` | list[HookConfig] | `[]` | `schema.py:2425` — see [Tool Hooks](31-tool-hooks.md) |
| `watchers` | bool | `false` | `schema.py:2430` |
| `scheduler` | bool | `false` | `schema.py:2434`. Requires `watchers: true`. |
| `default_channel` | string | `"llm_notification"` | `schema.py:2439` |
| `middleware` | list[dict] | `[]` | `schema.py:2444` — see [Middleware](17-middleware.md) |
| `pipeline` | list[PipelineStep] | `[]` | `schema.py:2453` — `mode=pipeline` only |
| `triggers` | list[TriggerConfig] | `[]` | `schema.py:2363` — see [Triggers](09-triggers.md) |
| `input`, `output` | InputConfig, OutputConfig | default-instances | `schema.py:2354, 2358` — `mode=one_shot` only |
| `payload_schema` | PayloadSchemaConfig\|None | `null` | `schema.py:2384` — `mode=background` only |

### `runtime.context` — Context window management

`schema.py:759` `ContextConfig` (`extra: forbid`). Eight fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_tokens` | int [0, 2_000_000] | `0` | `schema.py:784`. `0` = auto-detect from provider. |
| `output_reserved` | int | `4096` | `schema.py:793`. Reserved for output generation when computing pressure. |
| `strategy` | `truncate | summarize` | `summarize` | `schema.py:797` |
| `keep_recent` | int | `10` | `schema.py:801`. Most-recent messages preserved verbatim during compaction. |
| `compression_trigger` | float [0, 1] | `0.75` | `schema.py:805`. Pressure ratio that triggers auto-compaction. |
| `summary_max_tokens` | int | `1024` | `schema.py:809` |
| `auto_compact` | bool | `true` | `schema.py:813`. Auto-injects a `context_pressure` hook if none declared. |
| `summary_brain` | AgentBrain\|None | `null` | `schema.py:820`. Use a cheap/fast model for summaries instead of the agent's main brain. |

Per-agent override: each agent can re-declare `brain.context` with the
same fields.

## `agents:` — Agent definitions

`schema.py:2170` `AgentDefinition` (list-shape, `extra: forbid`). Full
field reference is on the [Agents](03-agents.md) page; here is the
shape and how it nests in the app:

```yaml
agents:
  - id: coordinator                 # Required, slug
    role: coordinator               # coordinator | specialist | worker | supervisor (see Agents doc)
    brain:                          # Required — see Agents doc for AgentBrain fields
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You are the coordinator.
    plan_first: true
    delegate_to: [explorer, writer]
    pool:                           # AgentPoolConfig — see Multi-Agent doc
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

## `tools:` — Modules, capabilities, channels

`schema.py:2472` `ToolsBlock` (`extra: forbid`).

```yaml
tools:
  modules:                          # dict[str, ModuleBlock] — keys are module ids
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
  channels:                          # dict[str, ChannelInstanceConfig] — see Channels doc
    slack_alerts:
      type: slack
      config: { ... }
```

### `tools.modules` — Module configuration

`schema.py:2483`. Map of module-id → `ModuleBlock` (`schema.py:665`).
Each `ModuleBlock` has 5 fields (`extra: forbid`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `config` | dict | `{}` | `schema.py:699`. Static config pushed via `module.on_config_update(config)` at bootstrap. Validated against the module's `CONFIG_MODEL` if declared. |
| `setup` | list[SetupStep] | `[]` | `schema.py:715`. Ordered actions executed at bootstrap. Each step = `{action: str, params: dict}`. |
| `constraints` | dict | `{}` | `schema.py:719`. Universal: `allowed_actions`, `blocked_actions`. Module-specific keys validated against the module's `ConstraintSpec`. |
| `middleware` | list[dict] | `[]` | `schema.py:727`. Module-level middleware pipeline. Example: `[{audit: {log_params: true}}, {retry: {max_attempts: 3}}]`. |
| `credential` | string \| dict \| null | `null` | `schema.py:734`. Compact: `credential: openai_main`. Explicit: `credential: { ref: openai_main, scope: per_user }`. Resolved at activation time. |

`SetupStep` (`schema.py:103`):
- `action: str` (required) — action name on the module
- `params: dict` (default `{}`) — may contain `{{variables}}`

The 22 modules shipped by the daemon are listed in
[the index](00-index.md#modules). Per-module reference docs live
under [modules/reference/](../modules/index.md). `context_builder`
and `llm_provider` are auto-loaded — never declare them.

### `tools.capabilities` — Grant / approve / deny

`schema.py:554` `CapabilitiesConfig` (`extra: forbid`). Optional
(`null` = dev/test mode, no enforcement). When present:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_policy` | `auto | approve | block` | `approve` | `schema.py:559` |
| `max_risk_level` | `low | medium | high` | `medium` | `schema.py:563` |
| `grant` | list[CapabilityGrant] | `[]` | Explicit allows |
| `approve` | list[CapabilityGrant] | `[]` | Each call pauses for user approval |
| `deny` | list[CapabilityGrant] | `[]` | Hard block |
| `approval_timeout` | int [30, 3600] | `300` | Seconds before auto-deny |
| `hidden_modules` | list[string] | `[]` | Modules hidden from the agent index but still callable from setup steps / hooks / channels |
| `hidden_actions` | list[CapabilityGrant] | `[]` | Specific actions hidden but executable internally |

`CapabilityGrant` (`schema.py:120`) is `{module: str, actions: list[str], reason: str}`. Empty `actions` = all actions on the module.

See [Security](11-security.md) for the resolution algorithm and
risk-level classification.

### `tools.channels` — Output channel instances

`schema.py:2491`. Map of channel-instance-name → `ChannelInstanceConfig`
(`schema.py:2109`). See [Channels (Bidirectional I/O)](40-channels.md)
for the full surface.

## `security:` — Runtime boundaries

`schema.py:2497` `SecurityBlock` (`extra: forbid`). All three sub-fields
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
| `behavior` | BehaviorConfig\|None | `schema.py:2508` | [Behavior Engine](43-behavior.md) |
| `sandbox` | SandboxConfig\|None | `schema.py:2515` | [OS Sandbox](35-sandbox.md) |
| `credentials_schema` | CredentialsSchemaConfig\|None | `schema.py:2522` | [credentials.md](../credentials.md) |

## `ui:` — Display layer (daemon never reads)

`schema.py:2532` `UIBlock` (`extra: forbid`). Pure client-side
rendering — every field here is consumed by the Flutter / web client,
not by the daemon.

```yaml
ui:
  theme: { accent: "#6EE7B7" }
  features: { voice: false, attachments: true }
  widgets:                            # see Widgets doc
    chat_side: [...]
  workspace:                          # virtual filesystem renderer
    render_mode: code
    entry_file: app.py
    title: Editor
  preview:                            # dev-server preview for apps shipping a web UI
    enabled: true
    command: vite
    port: 5173
  slash_commands:
    - command: /deploy
      description: Deploy to production
      template: "Deploy {{branch ?? 'main'}}"
  quick_prompts:                      # mirror of app.quick_prompts
    - label: "Counter"
      message: "Build a counter widget"
  greeting: "Hello! How can I help?"
```

| Field | Type | Default | Source |
|-------|------|---------|--------|
| `theme` | dict[str, str] | `{}` | `schema.py:2542`. Keys: `accent` (hex), `background` (hex). |
| `features` | dict[str, bool] | `{}` | `schema.py:2549`. Missing keys default to `true`. |
| `widgets` | WidgetsConfig\|None | `null` | `schema.py:2556` — see [Widgets](42-widgets.md) |
| `workspace` | WorkspaceBlock\|None | `null` | `schema.py:2560`. Renderer config (`render_mode`, `entry_file`, `title`). Distinct from `runtime.workdir` (FS path). See [Workspace & Preview](41-preview.md). |
| `preview` | PreviewConfig\|None | `null` | `schema.py:2569` — dev-server preview for apps shipping a web UI |
| `slash_commands` | list[SlashCommand] | `[]` | `schema.py:2573` |
| `quick_prompts` | list[QuickPrompt] | `[]` | `schema.py:2577` |
| `greeting` | string | `""` | `schema.py:2581`. Lifted from `ui.greeting`. |

`SlashCommand` (`typed_models.py:81`, `extra: allow`):
- `command: str` (required) — the `/foo` id
- `description: str` (default `""`)
- `template: str` (default `""`) — message template with `{{var}}` placeholders

## `dev:` — Developer affordances

`schema.py:2591` `DevBlock` (`extra: forbid`).

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

List of `SkillEntry` (`typed_models.py:53`, `extra: forbid`):
- `command: str` (required, min length 1) — slash command id
- `description: str` (default `""`) — one-line catalog entry
- `path: str` (required, min length 1) — path to the `.md` file
  relative to the bundle dir

The compiler reads the file at compile time and surfaces it via the
slash-command palette. See [Skills System](21-skills.md).

### `dev.variables`

`dict[str, str]`. Template substitutions exposed as `{{name}}` in
every other field of the YAML. Variables can reference each other
(max recursion depth 10, cycles detected). See **Variables** below.

### `dev.include` — Fragmentation

`IncludeBlock` (`typed_models.py:166`, `extra: forbid`). Splits
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

## `flow:` — Declarative orchestration graph (8th block)

`schema.py:2699` `AppDefinition.flow` (FlowConfig | None, default
`null`). Promoted to a **top-level block** in v2 because flow is a
paradigm shift: explicit scenography (a directed graph of nodes
with conditional edges) replaces implicit `Agent()`-tool
coordination.

`FlowConfig` is defined in `core/app/flow.py:279` (`extra: forbid`).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string (min 1) | yes | Flow identifier, unique within the app (`flow.py:310`). |
| `entry` | string (min 1) | yes | Starting node id (`flow.py:311`). |
| `description` | string | no | Free-form summary (`flow.py:312`). |
| `max_iterations` | int ≥ 0 | conditional | Per-flow cap on total node visits. `0` = no cap, only valid for acyclic flows. Required ≥ 1 when the graph has any cycle (`flow.py:313`). |
| `nodes` | list[FlowNode] (min 1) | yes | Nodes that compose the graph (`flow.py:322`). |

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
string in the YAML (`packages/digitorn/core/app/variables.py:103`
`resolve_variables`). Six namespaces, each with a fixed resolution
time.

| Namespace | Syntax | Resolved at | Source |
|-----------|--------|-------------|--------|
| User | `{{my_var}}` | Compile time | `dev.variables` |
| Environment | `{{env.VAR}}` | Compile time | `os.environ` |
| Secret | `{{secret.VAR}}` | Compile time | Encrypted DB, env fallback |
| System | `{{sys.VAR}}` | Compile time | Runtime introspection (see table below) |
| App | `{{app.FIELD}}` | Compile time | `app:` block metadata |
| Bundle file | `{{prompt.X}}`, `{{skill.X}}`, `{{behavior.X}}`, `{{asset.X}}` | Compile time | Bundle directory files |
| Runtime | `{{event.X}}`, `{{caller.X}}`, any other dotpath | Run time | Resolved by the consuming module |

### Fallback operator

```yaml
dev:
  variables:
    timeout: "{{env.TIMEOUT ?? '30'}}"
    region:  "{{env.AWS_REGION ?? 'eu-west-1'}}"
```

If the left side fails to resolve, the right side is used
(`variables.py:324-327`). Works with any namespace.

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

### Secrets (`{{secret.VAR}}`) — legacy

> **Prefer `credential:` blocks for new apps**
> ([credentials.md](../credentials.md)). The legacy
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

Or via REST: `PUT /api/apps/<app_id>/secrets/<KEY>` with body
`{"value": "..."}`.

The compiler emits a warning when an app uses
`{{secret.X}}` / `{{env.X}}` templates without a
`credential:` block. Run `digitorn yaml migrate-credentials
<file>` to migrate to the credentials vault.

### System variables (`{{sys.*}}`)

Resolved at compile time from `_SYS_VARIABLES`
(`variables.py:509`). The full list:

| Key | Source | Example |
|-----|--------|---------|
| `sys.timestamp` | `datetime.now(UTC).isoformat()` | `2026-05-01T18:30:00+00:00` |
| `sys.date` | `datetime.now(UTC).strftime("%Y-%m-%d")` | `2026-05-01` |
| `sys.time` | `datetime.now(UTC).strftime("%H:%M:%S")` | `18:30:00` |
| `sys.hostname` | `socket.gethostname()` | `prod-server-1` |
| `sys.platform` | `sys.platform` | `linux`, `darwin`, `win32` |
| `sys.os` | `platform.system()` | `Linux`, `Darwin`, `Windows` |
| `sys.arch` | `platform.machine()` | `x86_64`, `arm64` |
| `sys.python_version` | `platform.python_version()` | `3.13.12` |
| `sys.cwd` | `os.getcwd()` | `/home/user/apps` |
| `sys.user` | `$USER` / `$USERNAME` / `unknown` | `paul` |
| `sys.pid` | `os.getpid()` | `12345` |
| `sys.digitorn_version` | package version | `1.0.0` |
| `sys.home` | `~` expansion | `/home/paul` |
| `sys.tmpdir`, `sys.temp_dir` | `tempfile.gettempdir()` | `/tmp` |
| `sys.locale` | `$LANG` / `$LC_ALL` / `C` | `en_US.UTF-8` |
| `sys.shell` | detected default shell | `/bin/bash`, `pwsh` |
| `sys.shell_family` | shell category | `bash`, `pwsh`, `cmd` |
| `sys.path_sep` | `os.sep` | `/` or `\` |
| `sys.is_windows` | `"true"` / `"false"` | `"false"` |
| `sys.is_linux` | `"true"` / `"false"` | `"true"` |
| `sys.is_macos` | `"true"` / `"false"` | `"false"` |

Source of truth: `_SYS_VARIABLES` dict in `variables.py:509-532`.

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
preserved verbatim by the compiler. Modules resolve them at runtime —
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
The migration table is in [the index](00-index.md#migration-from-the-legacy-flat-shape).

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
  [OS Sandbox](35-sandbox.md), [credentials.md](../credentials.md)
- UI: [Client Manifest](44-client-manifest.md),
  [Widgets](42-widgets.md),
  [Workspace & Preview](41-preview.md)
- Dev: [Skills System](21-skills.md),
  [Bundle namespaces](38-bundle-namespaces.md)

> **Note**. Some content previously in this file (database
> auto-schema injection, business annotations, channel built-in
> types, sandbox detail) covers topics that belong in their dedicated
> reference docs ([modules/reference/database.md](../modules/reference/database.md),
> [40-channels.md](40-channels.md), [35-sandbox.md](35-sandbox.md)).
> Those sections are being relocated in a follow-up pass; this page
> is now strictly the 8-block configuration reference.
