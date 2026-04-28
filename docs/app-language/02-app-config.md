---
id: app-config
---

# App Configuration

The YAML file has 6 top-level blocks. Only `app:` and `agents:` are required.

## YAML Structure

```yaml
app:          # Required - application identity
variables:    # Optional - template variables
modules:      # Optional - module configuration
agents:       # Required - agent definitions (list)
channels:     # Optional - output channel instances
execution:    # Optional - runtime configuration
capabilities: # Optional - security configuration
```
## App Block

> **Scope note**: an app is deployed under a `(app_id, scope, owner_user_id)` triple. The YAML itself carries no scope field - the **deploy endpoint** picks one (`scope=system` by default, `scope=user` with the JWT's user_id for private per-user installs). See [Multi-Tenant Installs](45-multi-tenant.md).

```yaml
app:
  app_id: my-app                    # Required. Unique identifier
  name: "My Application"            # Required. Human-readable name
  version: "1.0"                    # Version string (default: "1.0")
  description: "What this app does" # Optional description
  author: "your-name"               # Optional author
  tags: [coding, assistant]          # Searchable tags
```
### Field Reference

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `app_id` | string | *required* | Unique application identifier |
| `name` | string | *required* | Human-readable name |
| `version` | string | `"1.0"` | Version string |
| `description` | string | `""` | Description |
| `author` | string | `""` | Author name |
| `tags` | list[string] | `[]` | Searchable tags |

## Variables

The `variables:` block defines reusable values accessible throughout the YAML via `{{variable_name}}`. Digitorn provides five variable namespaces, each resolved at the right time.

### Overview

| Namespace | Syntax | Resolved at | Source |
|-----------|--------|-------------|--------|
| **User** | `{{my_var}}` | Compile time | `variables:` block in YAML |
| **Environment** | `{{env.VAR}}` | Compile time | `os.environ` |
| **Secrets** | `{{secret.VAR}}` | Compile time | Encrypted DB, env fallback |
| **System** | `{{sys.VAR}}` | Compile time | System info (hostname, date, etc.) |
| **App** | `{{app.FIELD}}` | Compile time | `app:` block metadata |
| **Runtime** | `{{event.data.x}}` | Run time | Module-specific (channels, etc.) |

### User Variables

Define reusable values in the `variables:` block:

```yaml
variables:
  workspace: "{{env.PWD}}"
  max_file_lines: 500
  api_token: "{{env.MY_API_KEY}}"
  db_name: "{{app.id}}_production"
```
Variables are resolved everywhere in the YAML: `modules.*.config`, `modules.*.setup[].params`, `modules.*.constraints`, `agents[].brain.config`, `agents[].system_prompt`, `execution.*`.

```yaml
agents:
  - id: assistant
    system_prompt: |
      Application: {{app.name}} v{{app.version}}
      Working directory: {{workspace}}
      Max lines: {{max_file_lines}}
      Compiled at: {{sys.timestamp}}
```
### Environment Variables (`{{env.VAR}}`)

Read from `os.environ`. Raises a compilation error if the variable is not set.

```yaml
agents:
  - id: main
    brain:
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "{{env.LLM_BASE_URL ?? 'https://api.deepseek.com/v1'}}"
```
### Secrets (`{{secret.VAR}}`)

Two-step lookup: encrypted database first, `os.environ` fallback.

```yaml
modules:
  mcp:
    config:
      servers:
        notion:
          auth:
            client_id: "{{secret.NOTION_CLIENT_ID}}"
            client_secret: "{{secret.NOTION_CLIENT_SECRET}}"
agents:
  - id: assistant
    brain:
      config:
        api_key: "{{secret.OPENAI_API_KEY}}"
```
Secrets are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256), isolated per app, and never returned in plaintext by the API.

```bash
# Store secrets
digitorn secret set my-app API_KEY "sk-live-abc123"
digitorn secret set my-app API_KEY              # prompts (hidden input)
digitorn secret list my-app                      # list keys (values hidden)
digitorn secret delete my-app API_KEY

# Via API
curl -X PUT http://localhost:8000/api/apps/my-app/secrets/API_KEY \
  -H "Content-Type: application/json" \
  -d '{"value": "sk-live-abc123"}'
```

### System Variables (`{{sys.VAR}}`)

Computed at compile time. Useful for tagging builds, stamping reports, and adapting to the deployment environment.

| Variable | Example | Description |
|----------|---------|-------------|
| `{{sys.timestamp}}` | `2026-03-27T16:39:06+00:00` | ISO 8601 UTC compilation time |
| `{{sys.date}}` | `2026-03-27` | Date (YYYY-MM-DD) |
| `{{sys.time}}` | `16:39:06` | Time (HH:MM:SS) |
| `{{sys.hostname}}` | `prod-server-1` | Machine hostname |
| `{{sys.platform}}` | `linux` | OS platform (linux, darwin, win32) |
| `{{sys.os}}` | `Linux` | OS name |
| `{{sys.arch}}` | `x86_64` | CPU architecture |
| `{{sys.python_version}}` | `3.13.12` | Python version |
| `{{sys.cwd}}` | `/home/user/apps` | Current working directory |
| `{{sys.user}}` | `paul` | OS username |
| `{{sys.pid}}` | `12345` | Current process ID |
| `{{sys.home}}` | `/home/paul` | Home directory |
| `{{sys.tmpdir}}` | `/tmp` | Temp directory |
| `{{sys.locale}}` | `fr_FR.UTF-8` | System locale |
| `{{sys.digitorn_version}}` | `1.0.0` | Digitorn version |

**Example: stamped reports and environment-aware config**

```yaml
app:
  app_id: my-monitor
  name: "System Monitor"
  version: "2.0"
  author: "DevOps Team"

modules:
  filesystem:
    constraints:
      paths: ["{{sys.home}}/reports"]

  channels:
    config:
      providers:
        health_check:
          adapter: cron
          config:
            schedule: "0 9 * * 1-5"
          activation:
            context: |
              Report compiled at {{sys.timestamp}} on {{sys.hostname}} ({{sys.os}} {{sys.arch}})
              App: {{app.name}} v{{app.version}} by {{app.author}}
              Digitorn: {{sys.digitorn_version}}
              Python: {{sys.python_version}}
            message: "Generate the daily monitoring report."
```
### App Variables (`{{app.FIELD}}`)

Access metadata from the `app:` block. Resolved at compile time - useful for generating paths, tags, and context that reference the app identity.

| Variable | Source | Description |
|----------|--------|-------------|
| `{{app.id}}` | `app.app_id` | Application ID |
| `{{app.name}}` | `app.name` | Human-readable name |
| `{{app.version}}` | `app.version` | Version string |
| `{{app.author}}` | `app.author` | Author |
| `{{app.description}}` | `app.description` | Description |

```yaml
app:
  app_id: invoice-processor
  name: "Invoice Processor"
  version: "3.1"
  author: "Finance Team"

variables:
  data_dir: "/data/{{app.id}}"

modules:
  database:
    setup:
      - action: connect
        params:
          connection_id: main
          driver: sqlite
          database: "{{data_dir}}/{{app.id}}.db"

  filesystem:
    constraints:
      allowed_actions: [read, write, edit, glob, grep]

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You are {{app.name}} v{{app.version}}.
      Process invoices from {{data_dir}}.
```
### Runtime Variables (Passthrough)

Expressions with dotpaths that aren't `env.*`, `secret.*`, `sys.*`, or `app.*` are **preserved at compile time** and resolved at runtime by modules. This is how the channels module handles event data:

```yaml
modules:
  channels:
    config:
      providers:
        support:
          adapter: webhook
          config:
            inbound_path: "/hook/support"
          activation:
            prepare:
              - action: database.fetch_results
                params:
                  query: "SELECT * FROM clients WHERE phone = '{{event.source}}'"
                as: caller
            context: |
              Client: {{caller.name}} ({{caller.plan}})
              Source: {{event.source}}
            message: "{{event.payload.message}}"
```
These templates are resolved by the channels activation pipeline when an event arrives, not by the compiler. The prepare step result (`caller`) becomes available for subsequent templates.

| Pattern | Resolved by | When |
|---------|-------------|------|
| `{{event.payload.*}}` | Channels module | When an inbound event arrives |
| `{{event.source}}` | Channels module | Sender identifier (phone, email, IP) |
| `{{caller.*}}` | Channels pipeline | After a prepare step with `as: caller` |
| `{{any.dotpath.*}}` | Module runtime | Any dotpath is a runtime passthrough |

### Fallback with `??`

Use the `??` operator for optional variables:

```yaml
variables:
  timeout: "{{env.TIMEOUT ?? '30'}}"
  region: "{{env.AWS_REGION ?? 'eu-west-1'}}"
  greeting: "{{env.GREETING ?? 'Hello'}}"
```
If the left side cannot be resolved, the right side is used. Works with env, secret, and plain variables.

## Modules Block

The `modules:` block declares which modules to load and configures them.

```yaml
modules:

  # With constraints
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

  # With config and setup steps
  database:
    config:
      timeout_seconds: 10
    setup:
      - action: connect
        params:
          connection_id: main
          driver: sqlite
          database: "{{workspace}}/data.db"
    constraints:
      allowed_actions: [fetch_results, list_tables]
      blocked_actions: [execute_query]
```
### ModuleBlock Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `config` | dict | `{}` | Static module configuration, pushed via `on_config_update()` at bootstrap |
| `setup` | list[SetupStep] | `[]` | Ordered actions executed at bootstrap time |
| `constraints` | dict | `{}` | Runtime restrictions (`allowed_actions`, `blocked_actions`, module-specific) |

### SetupStep Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `action` | string | *required* | Action name on the module |
| `params` | dict | `{}` | Parameters (may contain `{{variables}}`) |

### Currently Implemented Modules

| Module | Description |
| ------ | ----------- |
| `hello` | Simple greeting module (test/demo) |
| `filesystem` | File read, list, find, grep, write, mkdir operations |
| `database` | Multi-driver database operations (SQLite, PostgreSQL, MySQL, MSSQL, Oracle, MongoDB, Redis) |
| `http` | HTTP client: GET, POST, JSON API, page fetch, download with progress tracking |
| `shell` | Shell execution: run commands, scripts, background processes, env/which |
| `llm_provider` | LLM provider management (auto-configured from brain) |
| `context_builder` | Tool discovery engine (system module, auto-loaded) |

> **Note**: The `context_builder` module is loaded automatically - you never declare it in `modules:`. The `llm_provider` module is auto-configured from the `brain:` block in each agent.

### Setup Steps and Pre-Configured Resources

When a module has `setup:` steps, they are executed at bootstrap time (app startup). The runtime automatically summarizes all successful setup steps and injects them into the agent's system prompt under a `# PRE-CONFIGURED RESOURCES` section.

This means the agent **knows what's already configured** without having to discover it. For example, with:

```yaml
modules:
  database:
    setup:
      - action: connect
        params:
          connection_id: main_db
          driver: postgresql
          host: db.example.com
          database: myapp
          password_env: DB_PASSWORD
```
The agent's system prompt will include:

```text
# PRE-CONFIGURED RESOURCES

The following resources were set up at startup and are ready to use:
- database.connect | connection_id=main_db | driver=postgresql | host=db.example.com | database=myapp | password_env=***

You do NOT need to configure these again - use them directly.
```

Sensitive fields (`password`, `password_env`, `api_key`, `secret`, `token`) are automatically redacted. If no module has setup steps, this section is not injected.

### Auto-Schema Injection (Database)

When the `database` module has active connections (from setup steps), the runtime automatically introspects all connected databases and injects the full schema into the agent's system prompt. The agent knows the table structure **from the first message** - no tool calls needed to discover the schema.

The schema includes:

- Table names and DB-native comments (`COMMENT ON` in PostgreSQL, column comments in MySQL)
- Column names, types, constraints (PK, NOT NULL)
- Foreign key relationships
- Business annotations (from YAML `annotate` steps - see below)

Example system prompt injection:

```text
DATABASE SCHEMA:

[main_db] (postgresql)
  users - Registered platform users
    - id INTEGER PK NOT NULL
    - name TEXT NOT NULL
    - email TEXT NOT NULL - Primary email, unique, used for authentication
    - created_at TIMESTAMP NOT NULL - Registration date
    FK: team_id -- teams.id
  orders - Customer orders
    - id INTEGER PK NOT NULL
    - user_id INTEGER NOT NULL - References users.id
    - total DECIMAL NOT NULL - Order total in cents
    - status TEXT NOT NULL - pending|confirmed|shipped|delivered
```

### Business Annotations

Use the `annotate` setup step to add business context to tables and columns. Annotations are prioritized over DB-native comments and give the agent a deep understanding of the data model.

```yaml
modules:
  database:
    setup:
      - action: connect
        params:
          connection_id: main_db
          driver: postgresql
          host: "{{env.DB_HOST}}"
          database: myapp
          password_env: DB_PASSWORD
          policy:
            preset: safe_write

      # Table-level annotation
      - action: annotate
        params:
          connection_id: main_db
          table: users
          description: "Registered platform users - one row per account"
          tags: [core, pii]

      # Column-level annotations
      - action: annotate
        params:
          connection_id: main_db
          table: users
          column: email
          description: "Primary email, unique, used for login and notifications"
          tags: [pii, unique]

      - action: annotate
        params:
          connection_id: main_db
          table: orders
          column: status
          description: "Order lifecycle: pending -- confirmed -- shipped -- delivered"
          tags: [enum]
```
Annotation fields:

| Field | Type | Description |
| ----- | ---- | ----------- |
| `description` | string | Business description (prioritized over DB comment) |
| `tags` | list[string] | Searchable tags (e.g. `pii`, `financial`, `immutable`) |
| `glossary` | dict | Business glossary (e.g. `{"SKU": "Stock Keeping Unit"}`) |
| `rules` | list[string] | Business rules (e.g. `"status transitions are one-way"`) |

**Priority**: YAML annotation > DB-native comment > empty. If the database already has `COMMENT ON` (PostgreSQL) or column comments (MySQL), they are used as fallback when no YAML annotation exists.

### Database High-Level Actions

In addition to `execute_query` and `fetch_results`, the database module provides optimized actions for common operations:

| Action | Risk | Description |
| ------ | ---- | ----------- |
| `bulk_insert` | medium | Insert multiple rows in one call. Provide `columns` + `rows` (array of arrays). Atomic transaction. |
| `batch_execute` | high | Execute multiple SQL statements in a single atomic transaction. All succeed or all roll back. |
| `upsert` | medium | Insert or update rows. If `conflict_columns` match an existing row, it updates instead of failing. |

These actions are **much faster** than calling `execute_query` repeatedly - they reduce tool calls from N to 1 and use transactional batching.

#### Upsert Example

```yaml
# The agent calls upsert with:
{
  "connection_id": "main_db",
  "table": "users",
  "columns": ["email", "name", "status"],
  "rows": [
    ["alice@example.com", "Alice Updated", "active"],
    ["bob@example.com", "Bob New", "pending"]
  ],
  "conflict_columns": ["email"],
  "update_columns": ["name", "status"]
}
```
Generates driver-appropriate SQL:

- **SQLite/PostgreSQL**: `INSERT ... ON CONFLICT (email) DO UPDATE SET name=EXCLUDED.name, status=EXCLUDED.status`
- **MySQL**: `INSERT ... ON DUPLICATE KEY UPDATE name=VALUES(name), status=VALUES(status)`
- **MSSQL**: `MERGE ... WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...`

All high-level actions enforce the same security layers: QueryGuard policy, table/column access control, audit logging, and transaction timeouts.

### Module Constraints

Universal constraints available for any module:

| Constraint | Type | Description |
| ---------- | ---- | ----------- |
| `allowed_actions` | list[string] | Whitelist of allowed action names |
| `blocked_actions` | list[string] | Blacklist of blocked action names |

Modules may declare additional constraints via their `ConstraintSpec` - use `digitorn app schema {module_id}` to see them.

## Discovering Module Schemas

Use the CLI to see what's available:

```bash
# List all available modules
digitorn app schema hello

# Shows:
#   - All actions with their parameter schemas
#   - All supported constraints
#   - Config fields (if any)
#   - YAML template for quick copy-paste
```

## Channels Block

The `channels:` block declares named output channel instances for delivering notifications from scheduled jobs, watchers, and background tasks. Channels are the notification delivery infrastructure - they route results to external systems (Slack, email, Kafka, webhooks, etc.).

```yaml
channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{env.SLACK_WEBHOOK_URL}}"
      headers:
        Content-Type: "application/json"

  audit_log:
    type: log
    config:
      logger_name: "digitorn.audit"
      level: "INFO"
      format: json
      include_data: true
```
### Channel Instance Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `type` | string | *required* | Channel type ID: `webhook`, `log`, or any installed plugin (`slack`, `telegram`, etc.) |
| `config` | dict | `{}` | Channel-specific configuration (supports `{{variables}}` and `{{env.VAR}}`) |
| `user_resolver` | object | `null` | Optional auto-resolution of per-user delivery targets (email, phone, chat_id) from a data source. See [Per-User Channel Resolution](05-channels.md) |
| `user_resolver.module` | string | *required* | Module ID to query (e.g. `database`, `http`) |
| `user_resolver.action` | string | *required* | Action to call on the module |
| `user_resolver.params` | dict | `{}` | Action parameters (`:session_id` is replaced with the user's session) |
| `user_resolver.mapping` | dict | `{}` | Maps result fields to per-delivery config fields |
| `user_resolver.cache_ttl` | float | `300` | Cache duration in seconds (0 = no cache) |

### Built-in Channel Types

| Type | Description |
| ---- | ----------- |
| `llm_notification` | Push to agent conversation (always available, no config needed) |
| `webhook` | HTTP POST to any URL (Slack, Discord, Teams, Zapier, n8n compatible) |
| `log` | Structured Python logging (debugging, audit trails) |

Plugin channels are installed via pip (`pip install digitorn-channel-slack`) and auto-discovered. See [Output Channels](05-channels.md) for the full channel system documentation.

## Execution Block

The `execution:` block configures runtime behavior.

> **Client-UI blocks** (`features:`, `theme:`, `slash_commands:`, `workspace_mode`, `quick_prompts`) read by the Flutter/web client are documented separately in the [Client Manifest Contract](44-client-manifest.md). They pass through the compiler unchanged and are exposed via `GET /api/apps/{id}`.

```yaml
execution:
  mode: conversation           # 'one_shot', 'conversation', or 'background'
  greeting: "Hello!"           # Greeting message (conversation mode)
  max_turns: 10                # Maximum agent loop iterations
  timeout: 120.0               # Total timeout in seconds
  entry_agent: assistant       # Which agent starts (multi-agent)
  workspace: "{{workspace}}"   # Working directory for file operations
  context:                     # Default context management for all agents
    max_tokens: 0
    strategy: summarize
    compression_trigger: 0.75
  hooks: []                    # Custom hooks (see Context Management)
  triggers: []                 # Triggers for background mode
  watchers: false              # Enable persistent monitoring (watch_* primitives)
  scheduler: false             # Enable scheduler + remember primitives
  # NOTE: workbench was removed. Use the `workspace` module for
  # real-time file lifecycle (see docs/modules/workspace.md).
  default_channel: llm_notification  # Default output channel for jobs/watchers
```
### Execution Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `mode` | string | `"one_shot"` | Execution mode: `one_shot`, `conversation`, or `background` |
| `entry_agent` | string | `""` | Agent to start with. Empty = first agent in list |
| `max_turns` | int | `50` | Max agent loop iterations (per turn for conversation, per activation for background) |
| `timeout` | float | `300.0` | Timeout in seconds (per turn for conversation, per activation for background) |
| `greeting` | string | `""` | Greeting message displayed at conversation start |
| `workspace` | string | `""` | Working directory for file operations. Resolution: (1) explicit value in YAML, (2) parent directory of the YAML source file, (3) CLI mode: current working directory, (4) daemon mode: managed directory under `~/.local/share/digitorn/workspaces/{app_id}/` |
| `input` | InputConfig | `InputConfig()` | Input contract (one_shot mode only) |
| `output` | OutputConfig | `OutputConfig()` | Output contract (one_shot mode only) |
| `context` | ContextConfig | `ContextConfig()` | Default context management for all agents (see [Context Management](06-context-management.md)) |
| `hooks` | list[HookConfig] | `[]` | Custom hooks (see [Context Management](06-context-management.md)) |
| `watchers` | bool | `false` | Enable persistent monitoring. When true, the agent gets `watch_*` primitives for periodic data source monitoring with smart escalation (see [Execution Primitives](04c-primitives.md)) |
| `scheduler` | bool | `false` | Enable time-based scheduling. When true, the agent gets `schedule_once`, `schedule_cron`, `schedule_cancel`, `schedule_list`, `schedule_status`, and `remember` primitives (see [Execution Primitives](04c-primitives.md)) |
| ~~`workbench*`~~ | - | - | **Removed.** Use the [`workspace` module](../../packages/digitorn/modules/workspace/docs/integration.md) for live file lifecycle. The preview module's `files` channel + diagnostics channel cover everything the old workbench did, with session-scoped persistence. |
| `default_channel` | string | `"llm_notification"` | Default output channel for scheduled jobs and watchers. Must reference a channel instance name from the `channels:` block, or `"llm_notification"` (always available). See [Output Channels](05-channels.md) |
| `triggers` | list[TriggerConfig] | `[]` | Triggers for background mode |

### Execution Modes

| Mode | Description |
| ---- | ----------- |
| `one_shot` | Process a single input and return. Uses `input`/`output` contracts. |
| `conversation` | Interactive multi-turn conversation. Uses `greeting`, `max_turns`. |
| `background` | Daemon mode, activated by triggers (cron, file watch). Uses `triggers`. |

### Input/Output Contracts (one_shot mode)

Define what your application accepts and produces.

**Input types:**

| Type | Description | Model requirement |
| --- | --- | --- |
| `text` | Plain text (default) | All models |
| `image` | Image file (PNG, JPEG, WebP) | Vision models (GPT-4o, Claude Sonnet, Gemini) |
| `audio` | Audio file (WAV, MP3, M4A) | Audio models (GPT-4o-audio, Gemini) |
| `video` | Video file (MP4) | Gemini |
| `file` | Any file (read via filesystem module) | All models |
| `json` | Structured JSON input | All models |
| `any` | Text, images, or files | Depends on model |

**Output types:**

| Type | Description | CLI behavior |
| --- | --- | --- |
| `text` | Plain text | Printed to stdout |
| `json` | Structured JSON | Pretty-printed, validated against schema |
| `markdown` | Markdown text | Rendered with Rich (headers, code blocks, tables) |
| `file` | File written to disk | Path printed to stdout |
| `image` | Generated image | Saved to file, path printed |
| `audio` | Generated audio | Saved to file, path printed |

**Examples:**

```yaml
# Text analysis with JSON output
execution:
  mode: one_shot
  input:
    type: text
    description: "Code to analyze"
    required: true
  output:
    type: json
    description: "Analysis report"
    schema:
      type: object
      properties:
        bugs: { type: array }
        score: { type: integer }

# Image analysis
execution:
  mode: one_shot
  input:
    type: image
    accept: ["image/png", "image/jpeg", "image/webp"]
    max_size: "10MB"
    description: "Image to analyze"
  output:
    type: json
    description: "Detected objects and description"

# Audio transcription
execution:
  mode: one_shot
  input:
    type: audio
    accept: ["audio/wav", "audio/mp3", "audio/m4a"]
    max_size: "50MB"
    description: "Audio to transcribe"
  output:
    type: text
    description: "Transcription"

# Conversation with image support
execution:
  mode: conversation
  input:
    type: any
    accept: ["image/png", "image/jpeg", "application/pdf"]
    description: "Text or images"

# Code generator with file output
execution:
  mode: one_shot
  input:
    type: text
    description: "Description of what to generate"
  output:
    type: file
    format: ".py"
    description: "Generated Python file"
```
**Input fields:**

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | string | `"text"` | Input type (text, image, audio, video, file, json, any) |
| `accept` | list | `[]` | Accepted MIME types. Empty = infer from type |
| `max_size` | string | `""` | Max input size (e.g. "10MB"). Empty = no limit |
| `description` | string | `""` | Human-readable description |
| `required` | bool | `true` | Whether input is mandatory |

**Output fields:**

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `type` | string | `"text"` | Output type (text, json, markdown, file, image, audio) |
| `format` | string | `""` | Format hint (.py, .svg, png, etc.) |
| `description` | string | `""` | Human-readable description |
| `schema` | object | `{}` | JSON Schema for output validation (json type only) |

### Sandbox Configuration

The optional `sandbox:` block inside `execution:` configures OS-level kernel isolation. See [OS-Level Sandbox](35-sandbox.md) for full details.

```yaml
execution:
  workspace: "./project"
  sandbox:
    level: strict              # off | standard | strict | maximum
    pool_size: 4               # pre-warmed workers (1-32)
    pool_max: 16               # max workers under load (1-64)
    namespaces: [user, pid, net]  # Linux namespaces
    workspace_snapshot: false   # CoW workspace per session
    audit: false               # per-session audit trail
    session_timeout: 3600      # max session duration (seconds)
    idle_timeout: 300          # idle before worker recycle
    allow_paths:               # additional filesystem access
      - /data/models           # read-only
      - ~/datasets:rw          # read-write
    resources:
      memory: "512MB"
      cpu: 2
      processes: 20
```
#### Sandbox Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `level` | string | `"standard"` | Preset: `off`, `standard`, `strict`, `maximum` |
| `pool_size` | int | `2` | Pre-warmed workers (strict/maximum only) |
| `pool_max` | int | `8` | Max workers under load |
| `namespaces` | list[string] | `[]` | Linux namespaces: `user`, `pid`, `net`, `mount` |
| `workspace_snapshot` | bool | `false` | CoW workspace snapshot per session |
| `audit` | bool | `false` | Per-session audit trail (JSONL) |
| `session_timeout` | int | `3600` | Max session duration in seconds |
| `idle_timeout` | int | `300` | Idle timeout before worker recycle |
| `allow_paths` | list[string] | `[]` | Additional paths: `path` (read-only), `path:rw` (read-write) |
| `resources` | dict | `{}` | Per-worker limits: `memory`, `cpu`, `processes` |

#### Sandbox Levels

| Level | What's enabled |
| ----- | -------------- |
| `off` | No sandbox |
| `standard` | Landlock + seccomp + hardening + cgroups |
| `strict` | + warm pool + user/PID namespaces + per-session isolation |
| `maximum` | + network namespace + seccomp-notify audit + workspace snapshots |

### Workspace

The `workspace` field sets the working directory for file operations. It supports template variables:

```yaml
variables:
  workspace: "{{env.PWD}}"

execution:
  workspace: "{{workspace}}"
```
If not set explicitly, the workspace defaults to: explicit value > YAML source file's parent directory > current working directory.

### Background Mode and Triggers

Background mode turns the app into a daemon that reacts to events. Each trigger activates the agent with a message.

```yaml
execution:
  mode: background
  triggers:
    # Run every hour
    - id: hourly_check
      type: cron
      schedule: "0 * * * *"
      message: "Hourly check: analyze recent changes."

    # Watch for new files
    - id: new_csv
      type: watch
      paths: ["./inbox/*.csv"]
      message: "New file detected: {{event.path}}"
```
#### Trigger Types

| Type | Required Fields | Description |
| ---- | --------------- | ----------- |
| `cron` | `schedule` | Cron expression (e.g., `"0 * * * *"` = every hour) |
| `watch` | `paths` | File glob patterns to watch for changes |
| `http` | `path`, `method` | HTTP endpoint trigger (not yet implemented) |

#### TriggerConfig Fields

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `id` | string | *required* | Unique trigger identifier |
| `type` | string | *required* | Trigger type: `cron`, `watch`, or `http` |
| `schedule` | string | `""` | Cron expression (cron type only) |
| `paths` | list[string] | `[]` | File glob patterns (watch type only) |
| `path` | string | `""` | HTTP endpoint path (http type only) |
| `method` | string | `"POST"` | HTTP method (http type only) |
| `message` | string | `""` | Message sent to the agent when triggered |

> **Note**: HTTP triggers are defined in the schema but not yet implemented in the runtime.

## Complete Example

This example uses all 6 top-level blocks and demonstrates most configuration options: variables with environment references, modules with config/setup/constraints, brain with context management and summary brain, execution with workspace and hooks, and capabilities with grants/denials.

```yaml
app:
  app_id: data-analyst
  name: "Data Analyst"
  version: "2.0"
  description: "AI data analysis assistant with read-only access."
  author: "digitorn"
  tags: [data, analysis, sql]

variables:
  workspace: "{{env.PWD}}"
  db_path: "{{workspace}}/data.db"
  api_key: "{{env.DEEPSEEK_API_KEY}}"

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]
  database:
    config:
      timeout_seconds: 30
    setup:
      - action: connect
        params:
          connection_id: main
          driver: sqlite
          database: "{{db_path}}"
    constraints:
      allowed_actions: [fetch_results, list_tables]

agents:
  - id: analyst
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      temperature: 0.2
      max_tokens: 8192
      config:
        api_key: "{{api_key}}"
      context:
        max_tokens: 80000
        output_reserved: 4096
        strategy: summarize
        keep_recent: 10
        compression_trigger: 0.75
        summary_max_tokens: 1024
        auto_compact: true
        summary_brain:
          provider: ollama
          model: qwen2.5:3b
          backend: openai_compat
    system_prompt: |
      You are a data analyst. Query databases and read files.
      Workspace: {{workspace}}

      WORKFLOW:
      1. list_categories -> see available modules
      2. browse_category(category="name") -> see module tools
      3. execute_tool(name="module.action", params={...}) -> execute

      IMPORTANT:
      - Go directly to execute_tool once you know the tool name.
      - Limit yourself to 3-5 tool calls per question.
      - If a tool fails, explain the error instead of retrying.

execution:
  mode: conversation
  greeting: "Data Analyst ready. Ask me about your data."
  workspace: "{{workspace}}"
  max_turns: 40
  timeout: 600.0
  hooks:
    - id: pressure_log
      on: turn_start
      condition:
        type: always
      action:
        type: log
        message: "Turn {turn}: ~{tokens} tokens, {messages} messages"
      cooldown: 0

capabilities:
  default_policy: auto
  max_risk_level: low
  grant:
    - module: filesystem
      actions: [read, glob, grep]
    - module: database
      actions: [fetch_results, list_tables]
  deny:
    - module: filesystem
      actions: [write, edit]
      reason: "Read-only mode"
    - module: database
      actions: [execute_query]
      reason: "Only fetch_results allowed"
```