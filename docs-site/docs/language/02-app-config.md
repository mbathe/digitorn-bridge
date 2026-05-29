---
id: app-config
---

# App Configuration

The canonical reference for the Digitorn app YAML. Every field on
this page is strictly enforced at compile time - unknown keys are
rejected.

## YAML structure (v2)

A canonical Digitorn app declares **eight top-level blocks** plus an
optional ``schema_version``:

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
> `digitorn yaml migrate-v2 path/to/app.yaml`. The compiler also keeps
> accepting legacy YAMLs (`execution:`, `modules:` at the top level, ...)
> by reshaping them via the alias pass before validation.
> See [the index migration table](/docs/language/#migration-from-the-legacy-flat-shape).

## `app:` - Identity

Identity, branding, and discovery metadata for the app.

```yaml
app:
  app_id: my-app                      # Required
  name: "My Application"              # Required
  short_name: "MyApp"                 # default "" (chip label, see below)
  version: "1.0"                      # default "1.0"
  description: "What this app does"   # default ""
  author: "your-name"                 # default ""
  tags: [coding, assistant]           # default []
  icon: "bot"                         # icon name, URL, data URI, or emoji
  color: "#8B5CF6"                    # hex; auto-generated if empty
  category: "coding"                  # default "general"
  attachments:                        # composer + menu (opt-in)
    - image
    - document
  quick_prompts:                      # one-click suggestions
    - label: "New PR"
      message: "Open a PR with the latest changes"
      icon: "rocket"
```

| Field | Type | Default |
|-------|------|---------|
| `app_id` | string | *required* |
| `name` | string | *required* |
| `short_name` | string | `""` |
| `version` | string | `"1.0"` |
| `description` | string | `""` |
| `author` | string | `""` |
| `tags` | list[string] | `[]` |
| `icon` | string | `""` |
| `color` | string | `""` |
| `category` | string | `"general"` |
| `attachments` | `list["image" \| "document" \| "audio" \| "video"]` or `"*"` or `null` | `null` (disabled) |
| `attachments_mode` | `"direct" \| "tool"` | `"direct"` |
| `quick_prompts` | list[QuickPrompt] | `[]` |

`QuickPrompt` is `{label*, message*, icon}` - `label` and `message` are required strings, `icon` defaults to `""`.

### `app.attachments` - what the composer's `+` menu accepts

Declares which attachment
types the chat composer will let the user upload. **Opt-in**:
when the field is unset (`null`) the composer hides the upload
entries entirely.

| Value | Effect |
|-------|--------|
| `null` / omitted | No attachments. Composer `+` menu collapses to slash-commands + snippets. **Default.** |
| `["image", "document"]` | Only the listed types appear in the menu. Order doesn't matter. |
| `"*"` | All four types enabled. Expanded server-side before the manifest reaches the client. |

Supported types and how the daemon routes each one:

| Type | Accepted extensions | Pipeline |
|------|--------------------|----------|
| `image`    | PNG, JPG, GIF, WEBP, HEIC | Embedded as base64, routed to a vision-capable LLM. Apps using a non-vision brain should disable. |
| `document` | PDF, DOCX, PPTX, ODT, ODS, XLSX, RTF, CSV, JSON, MD, TXT, HTML, XML, common code files | Format detected by magic bytes, parsed to plain text by the matching ingestor, then injected or indexed depending on `attachments_mode`. |
| `audio`    | MP3, WAV, M4A, OGG | Transcribed via the configured STT provider, the transcript is passed as text. |
| `video`    | MP4, MOV, WEBM | Sent to the LLM only when the model supports video (Gemini, recent Sonnet). Other models return an error. |

The client manifest (`GET /api/apps/{app_id}`) always exposes
this as a flat `attachments: [...]` array: `"*"` is expanded
server-side, `null` returns `[]`. UIs can read it once and
build the upload menu without reasoning about wildcards.

**Browser caps** (enforced client-side in the chat composer
and mirrored by `body.files[:10]` server-side):

| Cap | Value | Notes |
|-----|-------|-------|
| Per-file size | 10 MB | Larger files are rejected before upload starts. |
| Cumulative per message | 25 MB | Sum of all files attached to a single user message. |
| File count | 10 files | Extras dropped silently with a toast on the composer. |

```yaml
# Vision-only chatbot
app:
  attachments: [image]

# Full multimodal assistant
app:
  attachments: "*"

# Strict text-only app (default, same as omitting the field)
app:
  attachments: null
```

Adding a new attachment kind requires extending the daemon's
validator and expander tuples in lockstep.

### `app.attachments_mode` - how the agent sees attached files

Once a file has been
uploaded and parsed to text, this field decides what the
agent receives on the next turn.

| Mode | Effect | When to use |
|------|--------|-------------|
| `direct` | Full extracted text of every attached file is prepended to the user message. The agent answers immediately, no tool call needed. | **Default.** Chat apps without a workspace, small-doc Q&A where the user wants the model to "see" everything immediately. |
| `tool` | Files are mirrored into the workspace under `attachments/<name>`. The agent is told to call `WsRead` / `WsGlob` / `WsGrep` to inspect them. No content in the prompt. | Big-corpus apps where injecting the full text would blow the context window. Pair with [`workspace.agent_root: "attachments"`](../reference/modules/workspace.md#agent_root---scope-lock-for-attachments-mode) to lock the agent's view to the upload directory. |

`tool` mode needs the `workspace` module loaded or it silently
falls back to `direct`.

```yaml
# digitorn-chat - direct mode (real production app)
app:
  app_id: chat
  name: Chat
  attachments: [image, document]
  attachments_mode: direct

tools:
  modules:
    preview: {}
    workspace:
      config:
        render_mode: markdown
        agent_root: "attachments"      # agent can only see attachments/
        auto_approve: true
        lint: false
    rag: {}                            # daemon-internal, indexes uploads
  capabilities:
    default_policy: auto
    grant:
      - module: workspace
        actions: [read, glob, grep]    # read-only over attachments/
```

> **`short_name` - the dashboard chip label.** The home-page app picker
> renders each app as a 68 px wide chip with an icon and a one-line
> label underneath. `name` is shown everywhere else (manifest,
> sessions list, app card title), but for the chip the client falls
> back to `short_name` when set. Long names like `"Digitorn Deep
> Research"` overflow the 68 px slot and overlap their neighbours;
> `short_name: "Research"` keeps the chip tidy. **Rule of thumb: one
> word, or two SHORT words.** When omitted, the chip truncates `name`
> with an ellipsis, which still works but reads as `"Digitorn De..."`
> on long names. The Digitorn built-ins ship with: `Builder`, `Chat`,
> `Clone`, `Code`, `Copilot`, `Research`, `Sandbox`.

> **Mode picker.** The composer's Ask / Plan / Auto pill is driven by
> [`runtime.modes`](#runtime-modes-picker), not an
> AppMeta tag. Each entry is a structured override (system prompt,
> tool grants, behavior profile, …), not just a label.

> **Scope note**. Apps deploy under a `(app_id, scope, owner_user_id)`
> triple. The YAML carries no scope field - the deploy endpoint picks
> one (`scope=system` by default, `scope=user` from the JWT for
> private installs). See [Multi-Tenant Installs](45-multi-tenant.md).

> **Mirrors**. `app.features` and `app.theme` exist on the schema but
> are **deprecated** at this nested level - the canonical home is
> `ui.features` and `ui.theme`. The compiler lifts them with the
> alias pass; the migrator strips them.

## `runtime:` - Lifecycle and execution policy

Every field that
controls per-turn daemon behavior lives here.

```yaml
runtime:
  mode: conversation
  entry_agent: coordinator
  max_turns: 50
  timeout: 300.0
  modes:                             # default {} - composer mode picker
    ask:
      label: Ask
      description: Read-only Q&A
      max_turns: 8
      workspace_mode: none
      tool_grants:
        - module: filesystem
          actions: [read, glob, grep]
      behavior_profile: assistant
    plan:
      label: Plan
      description: Design first, edit after approval
      system_prompt: "Mode: Plan. Outline the steps, wait for approval."
      behavior_profile: coding
    auto:
      label: Auto
      description: Full-autonomy
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
| `modes` | dict[string, ModeDef] | `{}` |
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

### `runtime.modes` - Composer mode picker {#runtime-modes-picker}

Map of mode-id → mode definition. The chat composer surfaces the
picker only when at least two modes are declared - a single entry
(or empty dict) hides the pill entirely.

Each entry is a **sparse override**: only fields you set apply on
top of the app's normal runtime / agent / tools config when the
user picks that mode. Empty fields fall back to the app defaults.

```yaml
runtime:
  modes:
    ask:
      label: Ask                     # picker label, defaults to id capitalised
      description: Read-only Q&A     # subtitle in the dropdown (≤30 chars)
      icon: lightbulb                # lightbulb | map | sparkles | wrench | shield
      accent: cyan                   # primary | secondary | cyan | purple | red | green | orange
      max_turns: 8                   # override runtime.max_turns
      timeout: 60                    # override runtime.timeout
      workspace_mode: none           # override ui.workspace.mode
      system_prompt: |               # appended to the agent's system prompt
        Mode: Ask. Read-only investigation; do NOT write or run shell.
      tool_grants:                   # subset of tools.grant; empty = inherit all
        - module: filesystem
          actions: [read, glob, grep]
        - module: web
          actions: [search, fetch]
      behavior_profile: assistant    # override security.behavior.profile
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `label` | string | `""` | Picker label (falls back to id capitalised). |
| `description` | string | `""` | Dropdown subtitle. Keep it short. |
| `icon` | string | `""` | Picker icon hint. |
| `accent` | string | `""` | Pill border + dropdown row tint. |
| `max_turns` | int\|null | `null` | Override `runtime.max_turns`. Use `1` for one-shot. |
| `timeout` | float\|null | `null` | Override `runtime.timeout` in seconds. |
| `workspace_mode` | string\|null | `null` | Override `ui.workspace.mode`. |
| `system_prompt` | string | `""` | Suffix appended to the agent's system prompt. |
| `tool_grants` | list[CapabilityGrant] | `[]` | Subset of tools the agent can reach. Empty inherits everything. |
| `behavior_profile` | string | `""` | Override the behavior module profile. |

**Conventional ids.** Three names are wired into the client picker
with default icons + accents: `ask` (lightbulb / cyan), `plan`
(map / purple), `auto` (sparkles / green). Custom ids work too -
just set `label`, `icon` and `accent` explicitly.

**Built-in usage.** `digitorn-chat`, `digitorn-scribe`,
`digitorn-deepresearch`, `notes-lm` ship with no `runtime.modes`
(single dispatch path → no picker). `digitorn-code`,
`digitorn-builder` ship with `ask / plan / auto`.
`digitorn-lovable` ships with `plan / build`.

#### Runtime semantics - what fires when the user picks a mode

Once a mode is wired into `runtime.modes`, the daemon applies each
override at a specific point in the dispatch pipeline. Empty mode
fields are no-ops - the app default keeps its place.

1. **Mode_id arrives via the POST body.** The composer ships the
   selected mode in `POST /messages` as `{ "mode": "<id>" }`. When
   the body omits `mode`, the **default-policy** kicks in: `auto`
   if declared, else the first declared mode (insertion order),
   else no mode at all (every override is inert).

2. **Mode-switch system message** *(applied at every fresh user
   turn).* When the active mode differs from the session's stored
   mode, a durable system directive is injected into the
   conversation timeline, carrying:
   - the mode header `[Mode: <Label>]`
   - the YAML's `system_prompt` verbatim
   - the auto-generated tools-available + tools-blocked lists
   - the standing instruction "ask the user to switch mode if you
     need a blocked tool"

   The directive is persisted like assistant messages, survives
   daemon restarts, and is replayed on cold-load. The session's
   active mode is then bumped so the next turn with the same mode
   is a no-op.

3. **Tool list filtering** *(applied on the LLM call schema).*
   `tool_grants` is a strict allow-list. When non-empty, the agent
   computes an allowed / blocked partition over the app's full tool
   list and the LLM only sees the allowed tools in its schema for
   this turn. Empty `tool_grants` means full inheritance and no
   filtering.

4. **Tool dispatch guard** *(defense in depth).* Any call whose
   tool name is not in the active mode's allow-list is rejected
   with a synthetic error result:

   > Tool 'X' is blocked in mode 'Y'. Allowed tools: ... Ask the
   > user to switch to a mode that allows this tool. Do not retry
   > this call.

   This catches hallucinated calls (e.g. the LLM remembers a tool
   from a previous mode in the same conversation).

5. **`max_turns` / `timeout` caps** *(applied per turn).* When the
   mode narrows either value, the per-turn loop bound and timeout
   use the mode's value instead of the app's. A mode that caps
   `max_turns: 8` stops the inner loop at 8 even though the app
   declares 200.

6. **`behavior_profile` swap** *(applied per turn).* When the mode
   declares a profile, the behaviour engine re-resolves its active
   rules against the new profile while preserving per-session
   state (counters, sets, flags). Empty profile string reverts to
   the YAML's `security.behavior.profile`. A re-call with the same
   profile is a no-op.

7. **`workspace_mode` override** *(client signal, optional).* The
   client may hide the workspace pane in Ask mode etc. No
   server-side effect today.

#### Client surfaces

| Surface | Behaviour |
| --- | --- |
| Mode pill in the composer | Shown when the active app declares at least two modes. Pre-selected to the app's default mode (auto if declared, else the first declared). |
| Switch animation | A 600 ms colored pulse using the new mode's accent fires every time the user picks a different mode in the picker. |
| Session reload | The session API exposes the currently-bound mode so the picker comes back on the user's last-active mode, not the app default. |
| Directive bubbles | The mode-switch directives are NOT rendered as visible bubbles in the chat. The LLM still sees them as system messages in its context. |

#### Known limitations

- **Queue persistence.** Messages queued behind a long-running turn
  drop the mode on chain-drain - drained turns fall back to the
  default-policy mode. Fast-path POSTs (no queue) keep the user's
  selection.
- **Sub-agents.** `runtime.modes` applies to the coordinator turn
  only. Spawned sub-agents inherit the app's defaults regardless of
  the active mode. Per-mode sub-agent overrides are not in scope.
- **Per-mode credentials / brain.** Not supported. All modes share
  the agent's normal `brain` block.
- **Mid-turn mode change.** Once a turn is dispatched, the active
  configuration for that turn is frozen. Switching modes in the
  picker during a running turn affects only the next user message.

### `runtime.context` - Context window management

Eight fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_tokens` | int [0, 2_000_000] | `0` | `0` = auto-detect from provider. |
| `output_reserved` | int | `4096` | Reserved for output generation when computing pressure. |
| `strategy` | `truncate` \| `summarize` | `summarize` | Compaction strategy when the window fills. |
| `keep_recent` | int | `10` | Most-recent messages preserved verbatim during compaction. |
| `compression_trigger` | float [0, 1] | `0.75` | Pressure ratio that triggers auto-compaction. |
| `summary_max_tokens` | int | `1024` | Cap on the generated summary. |
| `auto_compact` | bool | `true` | Auto-injects a `context_pressure` hook if none declared. |
| `summary_brain` | AgentBrain\|None | `null` | Use a cheap/fast model for summaries instead of the agent's main brain. |

Per-agent override: each agent can re-declare `brain.context` with the
same fields.

## `agents:` - Agent definitions

List of agent definitions. Full
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

Tools declaration block.

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

Map of module-id to module block.
Each `ModuleBlock` has 5 fields:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `config` | dict | `{}` | Static config pushed to the module at bootstrap. Validated against the module's own config model when declared. |
| `setup` | list[SetupStep] | `[]` | Ordered actions executed at bootstrap. Each step = `{action: str, params: dict}`. |
| `constraints` | dict | `{}` | Universal: `allowed_actions`, `blocked_actions`. Module-specific keys validated against the module's constraint spec. |
| `middleware` | list[dict] | `[]` | Module-level middleware pipeline. Example: `[{audit: {log_params: true}}, {retry: {max_attempts: 3}}]`. |
| `credential` | string \| dict \| null | `null` | Compact: `credential: openai_main`. Explicit: `credential: { ref: openai_main, scope: per_user }`. Resolved at activation time. |

`SetupStep`:
- `action: str` (required) - action name on the module
- `params: dict` (default `{}`) - may contain `{{variables}}`

The 23 agent-facing modules shipped by the daemon are listed in
[the index](/docs/language/#modules). Per-module reference docs live
under [modules/reference/](../reference/modules/). `context_builder`
and `llm_provider` are auto-loaded - never declare them.

### `tools.capabilities` - Grant / approve / deny

Optional
(`null` = dev/test mode, no enforcement). When present:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_policy` | `auto` \| `approve` \| `block` | `approve` | Default approval policy for tools not listed elsewhere. |
| `max_risk_level` | `low` \| `medium` \| `high` | `medium` | Maximum risk level tools are allowed to declare. |
| `grant` | list[CapabilityGrant] | `[]` | Explicit allows |
| `approve` | list[CapabilityGrant] | `[]` | Each call pauses for user approval |
| `deny` | list[CapabilityGrant] | `[]` | Hard block |
| `approval_timeout` | int [30, 3600] | `300` | Seconds before auto-deny |
| `hidden_modules` | list[string] | `[]` | Modules hidden from the agent index but still callable from setup steps / hooks / channels |
| `hidden_actions` | list[CapabilityGrant] | `[]` | Specific actions hidden but executable internally |

`CapabilityGrant` is `{module: str, actions: list[str], reason: str}`. Empty `actions` = all actions on the module.

See [Security](11-security.md) for the resolution algorithm and
risk-level classification.

### `tools.channels` - Output channel instances

Map of channel-instance-name to channel config. See
[Channels (Bidirectional I/O)](40-channels.md)
for the full surface.

## `security:` - Runtime boundaries

All three sub-fields
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
| `behavior` | BehaviorConfig\|None | `null` | [Behavior Engine](43-behavior.md) |
| `sandbox` | SandboxConfig\|None | `null` | [OS Sandbox](35-sandbox.md) |
| `credentials_schema` | CredentialsSchemaConfig\|None | `null` | [credentials.md](../reference/runtime/credentials.md) |

## `ui:` - Display layer (daemon never reads)

Pure client-side rendering -
every field here is intended for the chat client / web client, not the
daemon.

The block ships **two layers**:

1. **Legacy** (kept for backward compatibility): `theme`, `features`,
   `widgets`, `workspace.render_mode`, `slash_commands`,
   `quick_prompts`, `greeting`.
2. **Chat layout / behaviour** (added 2026-05-04): `layout`,
   `density`, `thinking`, `tool_calls`, `composer`, `visual`, plus
   the extended `workspace` fields `position`, `width_pct`,
   `auto_open_on_first_tool`.

Every sub-block is **optional**; omitting it preserves the
historical client behaviour.

> **Wired vs reserved.** Not every documented field is consumed by
> the current web client. The tables below mark each field as either
> **Wired** (read at runtime, changes the UI) or **Reserved** (parsed
> and stored but ignored by the current premium composer; kept so
> apps that set it don't fail validation and so we can wire it in
> later releases without a schema bump). Setting a reserved field is
> a no-op on the web client; the YAML still validates.

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
    auto_open_on_first_tool: true     # Lovable-style auto-open (default)
    default_open: false               # pre-open the workspace pane on session mount
    default_view: auto                # auto|code|preview|changes|activity
    hidden_views: []                  # subset of [code, preview, changes, activity]
    preview_chrome:                   # toolbar above the preview iframe
      enabled: true                   # master switch (disable for a bare iframe)
      refresh: true                   # refresh button
      open_in_new_tab: true           # external-link button (auto-suppressed for bundled URLs)
      viewport_toggle: false          # mobile / tablet / desktop preset toggle
      url_bar: auto                   # auto|always|never (auto = show once ≥ 2 routes seen)

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
      icon: search
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
    inject_intent: false              # prepend an `intent` field to every tool schema
    hide_details: false               # only when inject_intent: hide the chevron entirely

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

| Field | Type | Default | Status | Description |
| --- | --- | --- | --- | --- |
| `theme.accent` | hex string | `""` | Reserved | The active accent today is sourced from `visual.accent`, falling back to `app.color`. |
| `theme.background` | hex string | `""` | Reserved | Client theming hook, no consumer today. |
| `widgets` | `WidgetsConfig \| null` | `null` | Wired | See [Widgets](42-widgets.md). |
| `slash_commands` | `list[SlashCommand]` | `[]` | Wired | `/`-palette entries. Same shape as `ui.slash_commands` further down (preferred location). |
| `quick_prompts` | `list[QuickPrompt]` | `[]` | Wired | Mirror of `app.quick_prompts`; the client merges both. |
| `greeting` | `str` | `""` | Reserved | Cut from the empty-state hero in a 2026-05-07 refresh that kept only the app name + quick prompts + composer. The field is still parsed; a future client release may surface it again. |

### Legacy `ui.features` (12 toggles)

`dict[str, bool]`, default `{}`. Missing keys default to `true`. The
new premium composer honours the toggles below; the rest are
reserved.

| Toggle | Status | Effect |
| --- | --- | --- |
| `voice` | Wired | AND-combined with `ui.composer.voice`. Either being `false` hides the mic button. |
| `attachments` | Wired | AND-combined with `ui.composer.file_upload`. Either being `false` hides the upload entry of the `+` menu. |
| `snippets` | Wired | Hides the "Insert snippet" entry of the `+` menu when `false`. |
| `context_ring` | Wired | Hides the context-pressure ring icon button when `false`. |
| `slash_commands` | Wired | AND-combined with `ui.composer.slash_commands`. Either being `false` hides the `/` palette entry of the `+` menu. |
| `tools_panel` | Reserved | The premium composer no longer ships a tools-panel button; toggle has no effect. |
| `tasks_panel` | Reserved | Same as above for tasks. |
| `memory_panel` | Reserved | No memory panel surface today. |
| `markdown` | Reserved | Markdown rendering is always on; toggling to `false` is a no-op. |
| `message_actions` | Reserved | Per-message action buttons not yet gated by this toggle (see `ui.message_actions` for the typed declaration). |
| `status_pills` | Reserved | Status pills always render. |
| `token_badges` | Reserved | Token badges always render. |

### Workspace block (`ui.workspace`)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `render_mode` | `str` | `"auto"` | `react` \| `html` \| `markdown` \| `slides` \| `code` \| `latex` \| `builder` \| `auto`. Auto-detects from the first file. |
| `entry_file` | `str \| null` | `null` | Default file the renderer opens. |
| `title` | `str \| null` | `null` | Workspace toolbar label. |
| `position` | `str` | `"right"` | `right` \| `bottom` \| `hidden` \| `overlay`. Where the pane sits relative to chat. |
| `width_pct` | `int (10..90)` | `50` | Workspace width as a percentage of the chat-vs-workspace split. Ignored when `position` is `hidden` / `overlay`. |
| `auto_open_on_first_tool` | `bool` | `true` | When `true` (default), the client opens the workspace pane the first time the agent writes a file or emits a `workbench_*` event (Lovable-style). Set to `false` for chat-only apps that should not surface a renderer just because a tool wrote one log. |
| `default_open` | `bool` | `false` | When `true`, the client opens the workspace pane IMMEDIATELY on session mount, before any agent action. Right for Lovable-style apps where the workspace IS the product surface. Independent of `auto_open_on_first_tool`. |
| `default_view` | `str` | `"auto"` | Which view the workspace opens on: `code` \| `preview` \| `changes` \| `activity` \| `auto`. `auto` picks `preview` when `render_mode` is anything other than `code`, else `code`. |
| `hidden_views` | `list[str]` | `[]` | Subset of `["code", "preview", "changes", "activity"]` to remove from the workspace mode menu. Right for hiding Monaco on apps where the user should never see the editor, or hiding Changes on auto-approve sandboxes. The remaining views still render normally. |
| `preview_chrome` | `PreviewChromeBlock` | see below | Per-feature flags for the toolbar above the preview iframe (refresh, open-in-new-tab, viewport toggle, URL bar). |

### `PreviewChromeBlock`

The chrome controls live inline in the workspace toolbar (next to the
mode menu) and stream their state through `usePreviewChromeStore` on
the client. Defaults are conservative; Lovable-style apps typically
enable every flag.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `true` | Master switch. `false` hides the entire chrome (bare iframe). |
| `refresh` | `bool` | `true` | Refresh button (forces an iframe remount via a nonce). |
| `open_in_new_tab` | `bool` | `true` | External-link button. Auto-suppressed for daemon-bundled URLs (`/api/apps/.../web-static/...`) where opening in a new tab would just hit the daemon route. |
| `viewport_toggle` | `bool` | `false` | Mobile (375px) / Tablet (768px) / Desktop preset toggle. Caps the iframe wrapper's `max-width`. |
| `url_bar` | `str` | `"auto"` | `auto` \| `always` \| `never`. `auto` reveals the URL pill once the iframe app has reported `digi:route-change` for at least two distinct routes (signals an SPA with routing). |

### Chat layout / behaviour blocks (optional, added 2026-05-04)

All sub-blocks below are strictly validated - unknown fields are
rejected at deploy time. Omit any of them to keep the client's
historical defaults.

#### `ui.layout` (Reserved)

`str`, default `"default"`. Allowed: `default`, `code`, `builder`,
`research`, `minimal`, `lovable`.

High-level preset intended to pre-fill any sub-block the YAML did
not define. Parsed by the client but **not consumed by the current
premium composer** — no preset-driven default kicks in today.
Setting it is a no-op until a future client release wires the preset
cascade. Until then, every sub-block (`thinking`, `tool_calls`,
`composer`, ...) is read from its own typed values.

#### `ui.density` (Wired)

`str`, default `"comfortable"`. Allowed: `compact`, `comfortable`.
Controls message-bubble spacing — compact halves the vertical gap
between bubbles, comfortable keeps the default spacing.

#### `ui.thinking` (Wired)

- `visible: bool` (default `true`) - when `false`, thinking blocks
  are hidden entirely.
- `collapsed_default: bool` (default `true`) - initial collapsed
  state of thinking blocks.

#### `ui.tool_calls` (Wired)

Controls how tool calls are rendered in the chat stream: the
standard chip view, a Lovable-style "verb shimmer", or a minimal
narrative-only surface.

- `collapsed_default: bool` (default `true`) - initial collapsed
  state of tool-call chips in the standard renderer.
- `show_silent: bool` (default `false`) - when `true`, plumbing
  tools (memory ops, `agent_spawn` internals, discovery
  meta-tools) are rendered. Default hides them to keep the
  stream readable.
- `inject_intent: bool` (default `false`) - when `true`, the
  context builder prepends a required `intent` field to every
  tool's input schema, the model fills it with a one-line
  human-readable verb ("Reading config.yaml", "Searching the
  web for ..."), and the frontend renders a progressive line
  with that verb shimmering instead of the chip. Trade-off:
  ~10-20 extra tokens per tool call; works on any tool-using
  model without per-tool changes.
- `hide_details: bool` (default `false`) - only meaningful
  when `inject_intent: true`. When `true`, the progressive
  intent line renders with NO chevron and NO expandable
  detail block. The user sees just the shimmering verb and
  that is the whole tool-call surface; per-tool params,
  results, and diffs are unreachable from the UI. Use for
  brand surfaces where the user should only follow the
  agent's narrative and never inspect raw tool plumbing
  (consumer apps, demo surfaces). No effect when
  `inject_intent` is false.
- `strict_mode: bool` (default `false`) - Lovable-style
  "strict mode": every assistant content block (intermediate
  text, thinking, tool calls) is rendered as a single
  shimmering phrase, EXCEPT blocks the user must read - the
  final answer (revealed in clear as it streams) and any text
  that immediately precedes a user-facing interaction
  (`ask_user` tool call). When `inject_intent` is on, tool
  calls keep their own auto-declared intent line; `strict_mode`
  extends the shimmer surface to text and thinking blocks too,
  driven by [`intent_phrases`](#uitool_callsintent_phrases).
  Strictly opt-in: when `false`, no LLM phrase call is fired
  and no per-turn overhead is incurred (the runtime gates the
  whole dispatch behind this flag). The same gate also keeps
  sub-agents fully out of the path - their `AgentContext` is
  built directly (not through bootstrap) so `_chat_tool_calls`
  is never stashed and the dispatcher short-circuits.
- `intent_phrases: IntentPhrasesConfig` (default factory) -
  Configures how the shimmer phrases are produced when
  `strict_mode: true`. See the sub-section below. Ignored when
  `strict_mode: false`.

Rendering matrix:

| `inject_intent` | `hide_details` | `strict_mode` | What the chat surface renders |
|-----------------|----------------|---------------|-------------------------------|
| `false`         | n/a            | `false`       | `DetailedToolCallGroup`: standard chip with spinner, summary, and chevron to expand params + result. Text and thinking blocks render in clear. |
| `true`          | `false`        | `false`       | `ProgressiveGroup`: shimmering verb line for tool calls (chevron stays). Text and thinking blocks render in clear. |
| `true`          | `true`         | `false`       | `ProgressiveGroup` minimal: shimmering verb only, no chevron. Text and thinking still in clear. |
| `true`          | `true`         | `true`        | Full Lovable strict mode: tool calls shimmer (their own intent line), intermediate text and thinking blocks shimmer (using `intent_phrases`), the final answer streams in clear, and any text preceding an `ask_user` is also revealed so the user has the question's context. |

```yaml
# Standard chip renderer (e.g. internal agent surfaces)
ui:
  tool_calls:
    collapsed_default: true
    show_silent: false

# Tool-call intent only (lightweight Lovable feel, text still in clear)
ui:
  tool_calls:
    inject_intent: true
    hide_details: true

# Full Lovable strict mode (consumer / demo surfaces)
ui:
  tool_calls:
    inject_intent: true
    hide_details: true
    strict_mode: true
    intent_phrases:
      source: auto                  # LLM with static fallback
      llm:
        gateway_model: gpt-4o-mini  # any gateway-routable model
        timeout_seconds: 4
      static:
        phases:
          analyzing:    ["Analyzing your request..."]
          thinking:     ["Thinking..."]
          tool_streaming: ["Working on it..."]
          between_tools: ["Reviewing results..."]
          finalizing:   ["Wrapping up the response..."]
```

##### `ui.tool_calls.intent_phrases`

`IntentPhrasesConfig`. Sources
the shimmer phrases for `strict_mode`. Three modes:

- `source: "llm" | "static" | "auto"` (default `"auto"`) -
  where the phrases come from. `auto` tries the LLM first and
  falls back to `static` on timeout / error / empty result.
  `llm` never falls back (emits an empty list on failure, the
  frontend then uses its own client-side default cycle).
  `static` skips the LLM entirely (zero outbound cost).
- `llm: IntentPhrasesLLMConfig` - LLM-driven generator. Fires
  ONE cheap call at turn start, ALWAYS through the gateway
  (the daemon never talks to a provider directly - the
  gateway handles credentials, quota, and cost tracking even
  for these tiny side calls).
  - `gateway_model: str` (default `"claude-haiku-4-5"`) -
    gateway alias resolved by the gateway catalogue. Pick a
    model your gateway actually routes (e.g. `gpt-4o-mini`,
    `copilot-gpt-4o-mini` for the free GitHub Copilot route,
    a Gemini Flash alias, ...). If the gateway returns 404
    `model_not_provided_by_digitorn` the call is treated as
    a failure and `auto` falls back to static.
  - `max_phrases: int` (default `6`, range `2..12`) - upper
    bound on the list. A chatty model can't bloat the SSE
    payload.
  - `min_phrases: int` (default `4`, range `1..12`) - target
    minimum (only used in the prompt template).
  - `timeout_seconds: float` (default `4.0`, range
    `0.5..30.0`) - hard cap on the gateway call. Past this
    the daemon abandons the LLM path and uses static (when
    `source=auto`).
  - `prompt: str` (default template) - prompt for the
    generator. `{user_message}`, `{min}`, `{max}` are
    substituted. Override per app to bias the style
    (technical, casual, branded vocabulary, etc.).
- `static: IntentPhrasesStaticConfig` - static fallback
  matrix.
  - `phases: dict[str, list[str]]` - phrases grouped by
    agent phase. Known keys (one picked per phase per turn):
    - `analyzing` - early streaming, before any tool call.
    - `thinking` - an open `thinking` block is streaming.
    - `tool_streaming` - a tool call is in flight without
      an LLM-declared intent.
    - `between_tools` - text between two tool calls.
    - `finalizing` - last segment before the final answer.
    Unknown keys are tolerated but unused.

The daemon emits a single `intent_phrases` SSE event at
turn start with `{phrases, source, correlation_id}`. The
frontend stores it keyed by `correlation_id` and cycles
through the list while rendering each shimmer block.
Possible `source` values you'll see in `~/.digitorn/logs/intent_phrases.log`:
`llm` (LLM call succeeded), `llm_empty` (succeeded but
returned no usable phrases), `static_fallback` (LLM
failed/timed out, used static), `static` (config asked for
static only).

```yaml
# Pure-static (zero LLM cost, deterministic phrases)
ui:
  tool_calls:
    strict_mode: true
    intent_phrases:
      source: static
      static:
        phases:
          analyzing: ["Looking at your request..."]
          thinking: ["Thinking it through..."]
          tool_streaming: ["Working on it..."]
          between_tools: ["Moving to the next step..."]
          finalizing: ["Almost there..."]

# LLM-only, no fallback (best on a healthy gateway, blank
# shimmer on failure)
ui:
  tool_calls:
    strict_mode: true
    intent_phrases:
      source: llm
      llm:
        gateway_model: copilot-gpt-4o-mini
        timeout_seconds: 3

# Branded prompt (re-uses the same generator, biased tone)
ui:
  tool_calls:
    strict_mode: true
    intent_phrases:
      source: auto
      llm:
        gateway_model: gpt-4o-mini
        prompt: |
          You speak as the Acme app builder. Generate {min}-{max}
          short '-ing' phrases (3-6 words each) in Acme's voice,
          one per micro-step the agent will take to answer.
          JSON array of strings only.

          Request: {user_message}
```

#### `ui.composer`

Mirrors the legacy `ui.features` flags for the same concepts. When
both are present, the typed `composer.X` wins.

- `file_upload: bool` (default `true`) - paperclip / drag-drop
  attachment.
- `voice: bool` (default `true`) - microphone button. Default
  is `true` to match the legacy `features.voice` default; set
  `composer.voice: false` explicitly to hide the mic.
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

`ActivityPanelBlock`. Opt-in pane
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

#### `ui.slots`

`SlotsConfig`. Five named
placements in the chat surface where the app can render an
inline widget. Each slot is optional; omitted slots stay
empty so existing apps without a `ui.slots` block keep their
default layout.

```yaml
ui:
  widgets:
    inline:
      session_meta: { type: text, value: "v1.4" }
      outline:      { type: list, ... }
      context:      { type: card, ... }
      branch_chip:  { type: text, value: "{{branch}}" }
      status_chip:  { type: badge, ... }

  slots:
    header:                          # floating overlay, no vertical cost
      kind: inline
      ref:  session_meta
    sidebar_left:                    # left of the message list
      kind: inline
      ref:  outline
    sidebar_right:                   # right of the message list
      kind: inline
      ref:  context
    footer_left:                     # REPLACES the workspace-path chip
      kind: inline
      ref:  branch_chip
    footer_right:                    # REPLACES the model-name chip
      kind: inline
      ref:  status_chip
```

| Slot | Where it renders | Vertical cost |
|------|------------------|---------------|
| `header`        | Top-right overlay above the chat panel | None (floating) |
| `sidebar_left`  | Left of the message list, inside the chat panel | Takes a column |
| `sidebar_right` | Right of the message list, inside the chat panel | Takes a column |
| `footer_left`   | **Replaces** the workspace-path chip in the StatusLine row below the composer | None |
| `footer_right`  | **Replaces** the model-name chip in the same StatusLine row | None |

Each slot is a `SlotEntry` with two fields:

- `kind: str` (default `"inline"`) - renderer type. Phase 1
  supports `inline` only. Phase 4 will add `chart`,
  `data_table`, `iframe` as native kinds.
- `ref: str` (default `""`) - name of the inline widget to
  render. Must exist in [`ui.widgets.inline.<ref>`](42-widgets.md)
  when `kind: inline`.

The footer pair is the "no-extra-row" override mechanism:
instead of adding a new line below the composer (rejected as
wasted vertical space), the YAML hijacks the two chips already
living in the StatusLine.

> There is **no `above_composer` slot**. Action rows between
> the message list and the composer were rejected as visually
> competing with both the scroll area and the composer
> itself. Apps that need pre-composer affordances should use
> `header` (overlay) or the upcoming `message_actions`
> (per-message buttons).

### Custom typed models

`SlashCommand`:

- `command: str` (required) - the `/foo` id
- `description: str` (default `""`)
- `template: str` (default `""`) - message template with `{{var}}`
  placeholders

`QuickPrompt`:

- `label: str` (required, min 1) - short button label
- `message: str` (required, min 1) - full prompt sent on click
- `icon: str` (default `""`) - emoji or icon name

## `dev:` - Developer affordances

Developer affordances block.

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

List of `SkillEntry`:
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

`IncludeBlock`. Splits list-shaped sections (`agents`, hooks) into
separate files. The compiler resolves these before validation.

```yaml
dev:
  include:
    agents: ./agents/                                     # directory of YAMLs
    hooks: [./hooks/lint.yaml, ./hooks/auto-test.yaml]    # explicit list
```

Convention: `./agents/*.yaml` and `./hooks/*.yaml` are auto-loaded
even without an explicit `include:` entry.

## `flow:` - Declarative orchestration graph (8th block)

`flow` (FlowConfig | None, default
`null`). Promoted to a **top-level block** in v2 because the model
is different from agent-driven coordination: a directed graph of
nodes with conditional edges, declared up front, instead of
runtime `Agent` tool calls.

`FlowConfig` definition.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string (min 1) | yes | Flow identifier, unique within the app. |
| `entry` | string (min 1) | yes | Starting node id. |
| `description` | string | no | Free-form summary. |
| `max_iterations` | int ≥ 0 | conditional | Per-flow cap on total node visits. `0` = no cap, only valid for acyclic flows. Required ≥ 1 when the graph has any cycle. |
| `nodes` | list[FlowNode] (min 1) | yes | Nodes that compose the graph. |

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
> which lifts it to top-level before
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

If the left side fails to resolve, the right side is used.
Works with any namespace.

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
> `{{secret.X}}` system still works as a fallback at runtime, but
> new apps should reference the centralised credentials vault by
> name.

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

Resolved at compile time. The full list:

| Key | Source | Example |
|-----|--------|---------|
| `sys.timestamp` | `datetime.now(UTC).isoformat` | `2026-05-01T18:30:00+00:00` |
| `sys.date` | `datetime.now(UTC).strftime("%Y-%m-%d")` | `2026-05-01` |
| `sys.time` | `datetime.now(UTC).strftime("%H:%M:%S")` | `18:30:00` |
| `sys.hostname` | `socket.gethostname` | `prod-server-1` |
| `sys.platform` | `sys.platform` | `linux`, `darwin`, `win32` |
| `sys.os` | `platform.system` | `Linux`, `Darwin`, `Windows` |
| `sys.arch` | `platform.machine` | `x86_64`, `arm64` |
| `sys.runtime_version` | Runtime version | `3.13.12` |
| `sys.cwd` | `os.getcwd` | `/home/user/apps` |
| `sys.user` | `$USER` / `$USERNAME` / `unknown` | `paul` |
| `sys.pid` | `os.getpid` | `12345` |
| `sys.digitorn_version` | package version | `1.0.0` |
| `sys.home` | `~` expansion | `/home/user` |
| `sys.tmpdir`, `sys.temp_dir` | `tempfile.gettempdir` | `/tmp` |
| `sys.locale` | `$LANG` / `$LC_ALL` / `C` | `en_US.UTF-8` |
| `sys.shell` | detected default shell | `/bin/bash`, `pwsh` |
| `sys.shell_family` | shell category | `bash`, `pwsh`, `cmd` |
| `sys.path_sep` | `os.sep` | `/` or `\` |
| `sys.is_windows` | `"true"` / `"false"` | `"false"` |
| `sys.is_linux` | `"true"` / `"false"` | `"true"` |
| `sys.is_macos` | `"true"` / `"false"` | `"false"` |

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
resolve to file content / URLs at compile time:

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

The compiler's alias pass accepts the legacy flat shape and reshapes
it to canonical before validation runs.
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
      icon: "bar-chart"

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
