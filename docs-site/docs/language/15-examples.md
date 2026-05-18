---
id: examples
---

# Examples

14 complete app YAMLs covering the canonical 8-block
structure: minimal chat, one-shot, context management, local
LLMs, security, multi-agent, background mode, execution
primitives, watchers + scheduler + channels, MCP servers,
OAuth (SSE), and OAuth (stdio).

Every example follows the canonical structure
(`AppDefinition`, `schema.py`, `extra: forbid`):

```
app:        # identity (id, name, version, ...)
runtime:    # mode, max_turns, triggers, hooks, watchers, payload_schema, ...
agents:     # who runs (brain + system_prompt + per-agent hooks)
tools:      # modules, capabilities, channels
security:   # behavior, sandbox, credentials_schema
ui:         # theme, features, slash_commands, widgets
dev:        # variables, secrets (dev only)
flow:       # FlowConfig (multi-app pipelines)
```

> **Drift warning.** The legacy `execution:` block was renamed
> to `runtime:`. Old YAMLs are still accepted via
> `schema_aliases.py`, but new apps should write `runtime:`
> directly. `channels:` lives under `tools:` (NOT at top
> level).

## 1 · Minimal chat

The simplest possible conversational app - LLM + filesystem
read access.

```yaml
app:
  app_id: chat-assistant
  name: Chat Assistant
  description: Interactive conversation with tool access.

runtime:
  mode: conversation

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{secret.DEEPSEEK_API_KEY}}"
        base_url: https://api.deepseek.com/v1
    system_prompt: |
      Tu es un assistant intelligent et amical. Tu réponds en français.
      Utilise les outils disponibles quand c'est pertinent.

tools:
  modules:
    filesystem: {}
  capabilities:
    default_policy: auto

ui:
  greeting: "Bienvenue ! Je suis ton assistant Digitorn."
```

## 2 · One-shot task

Process a single input and return; the agent loop exits as
soon as the LLM produces text without calling any tool.

```yaml
app:
  app_id: hello-oneshot
  name: Hello One-Shot

runtime:
  mode: one_shot
  input:
    type: text
    description: A question or message
  output:
    type: text

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config: {api_key: "{{secret.DEEPSEEK_API_KEY}}"}
    system_prompt: "Be concise and helpful. Use tools when relevant."

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
  capabilities:
    default_policy: auto
```

```bash
digitorn app run hello-oneshot.yaml
digitorn dev chat hello-oneshot -m "Say hello in 3 languages"
```

## 3 · Smart chat with context management

Conversation mode with automatic context compaction
(`summarize` strategy + `auto_compact: true`).

```yaml
app:
  app_id: smart-chat
  name: Smart Chat

runtime:
  mode: conversation
  max_turns: 40
  timeout: 1200

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config: {api_key: "{{secret.DEEPSEEK_API_KEY}}"}
      context:
        max_tokens: 80000
        output_reserved: 1000
        strategy: summarize           # truncate | summarize
        keep_recent: 20               # last N messages always kept verbatim
        compression_trigger: 0.9      # compact when usage > 90 %
        summary_max_tokens: 5120
        auto_compact: true
    system_prompt: |
      Tu es un assistant intelligent. Réponds en français.
      Limite-toi à 3-5 appels d'outils maximum par question.
      Si un outil échoue, explique l'erreur, ne ré-essaie pas en boucle.

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep, write]
  capabilities:
    default_policy: auto

ui:
  greeting: "Salut ! Assistant avec gestion automatique du contexte."
```

## 4 · Local LLM (Ollama, no native tools)

Local model, tool definitions injected in the system prompt
since the model doesn't support native function calling.

```yaml
app:
  app_id: ollama-chat
  name: Ollama Chat

runtime:
  mode: conversation
  max_turns: 10
  timeout: 300

agents:
  - id: assistant
    role: assistant
    brain:
      provider: ollama
      model: qwen2.5:14b-instruct-q4_K_M
      backend: openai_compat
      config: {base_url: http://localhost:11434/v1}
      context:
        max_tokens: 8000
        strategy: truncate
        keep_recent: 6
        compression_trigger: 0.6
        auto_compact: true
    system_prompt: "Tu es un assistant local. Limite-toi à 3-5 appels d'outils max."

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
  capabilities:
    default_policy: auto

ui:
  greeting: "Assistant local prêt."
```

**Local-LLM tips**: `timeout: 300` (or higher) - local
models are slower than cloud APIs. If your model supports
native tool calling (e.g. `qwen2.5-coder`), set
`native_tool_use: true` on the brain for much better tool
reliability:

```yaml
brain:
  provider: ollama
  model: qwen2.5-coder:7b
  native_tool_use: true
```

## 5 · Context-management stress test

Aggressive `compression_trigger` so the compaction code path
fires within a few turns. Includes a `runtime.hooks:` entry
that logs token pressure on every turn.

```yaml
app:
  app_id: context-test
  name: Context Management Test

runtime:
  mode: conversation
  max_turns: 50
  timeout: 120
  hooks:
    - id: pressure_log
      "on": turn_start
      condition: {type: always}
      action:
        type: log
        message: "Turn {turn}: ~{tokens} tokens, {messages} messages"
      cooldown: 0

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config: {api_key: "{{secret.DEEPSEEK_API_KEY}}"}
      context:
        max_tokens: 0                 # 0 = use provider default
        output_reserved: 4096
        strategy: summarize
        keep_recent: 6
        compression_trigger: 0.15     # very aggressive
        summary_max_tokens: 512
        auto_compact: true
    system_prompt: "Réponds en français. Sois détaillé pour générer du contenu."

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
  capabilities:
    default_policy: auto
```

## 6 · Smart chat with summary brain

Same as example 3 but the **summary brain** is a small local
model - keeps the main conversation on the cloud LLM while
compaction runs locally for free.

```yaml
agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config: {api_key: "{{secret.DEEPSEEK_API_KEY}}"}
      context:
        max_tokens: 80000
        strategy: summarize
        keep_recent: 10
        compression_trigger: 0.75
        summary_max_tokens: 1024
        auto_compact: true
        summary_brain:
          provider: ollama
          model: qwen2.5:3b
          backend: openai_compat
          config: {base_url: http://localhost:11434/v1}
    system_prompt: "Réponds en français."
```

## 7 · Secure read-only analyst

Read-only filesystem + database access, with explicit
`grant:` / `deny:` and `max_risk_level: low`. Demonstrates
the security model in [Security](11-security.md).

```yaml
app:
  app_id: secure-analyst
  name: Secure Analyst

runtime:
  mode: conversation
  workdir: "{{env.PWD}}"

agents:
  - id: analyst
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config: {api_key: "{{secret.DEEPSEEK_API_KEY}}"}
    system_prompt: |
      You are a data analyst with read-only access.
      Never attempt to modify files or execute write queries.

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
    database:
      setup:
        - action: connect
          params:
            connection_id: main
            driver: sqlite
            database: "{{workspace}}/data.db"
      constraints:
        allowed_actions: [fetch_results, list_tables]
  capabilities:
    default_policy: auto
    max_risk_level: low
    grant:
      - {module: filesystem, actions: [read, glob, grep]}
      - {module: database,   actions: [fetch_results, list_tables]}
    deny:
      - {module: filesystem, actions: [write, edit], reason: "Read-only mode"}
      - {module: database,   actions: [execute_query], reason: "Only fetch_results allowed"}

ui:
  greeting: "Data analyst ready. I can read files and query databases."

dev:
  variables:
    workspace: "{{env.PWD}}"
```

## 8 · Multi-agent (coordinator + worker)

Two agents with **different providers**. The coordinator
delegates to the worker via `Agent(prompt=..., wait=true)`.

```yaml
app:
  app_id: multi-agent
  name: Multi-Agent

runtime:
  mode: conversation
  entry_agent: coordinator
  max_turns: 30
  workdir: "{{env.PWD}}"

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config: {api_key: "{{secret.DEEPSEEK_API_KEY}}"}
      context: {strategy: summarize, keep_recent: 20, compression_trigger: 0.7}
    system_prompt: "You orchestrate tasks and delegate to workers via Agent()."

  - id: worker
    role: worker
    brain:
      provider: groq
      model: llama-3.3-70b-versatile
      backend: openai_compat
      config:
        api_key: "{{secret.GROQ_API_KEY}}"
        base_url: https://api.groq.com/openai/v1
      context: {strategy: truncate, keep_recent: 10}
    system_prompt: "You execute tasks assigned by the coordinator."

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
  capabilities:
    default_policy: auto

ui:
  greeting: "Multi-agent system ready."
```

The `Agent` tool is the single primitive with 8 modes (see
[Agent Spawn](03-agents.md)). The coordinator gets `Agent`
automatically; sub-agents do not, by default.

## 9 · Background mode with triggers

A daemon app that watches for new CSV files and a cron job
that emits an hourly summary. See [Triggers](09-triggers.md)
for routing semantics.

```yaml
app:
  app_id: csv-watcher
  name: CSV Watcher

runtime:
  mode: background
  max_turns: 10
  timeout: 60
  workdir: "{{env.PWD}}"
  triggers:
    - id: new_csv
      type: watch
      paths: ["{{workspace}}/inbox/*.csv"]
      message: "New CSV file: {{event.path}}. Analyze it."
      routing: broadcast

    - id: hourly_report
      type: cron
      schedule: "0 * * * *"
      message: "Generate an hourly summary of {{workspace}}/inbox/."
      routing: broadcast

agents:
  - id: analyst
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config: {api_key: "{{secret.DEEPSEEK_API_KEY}}"}
    system_prompt: |
      When activated, read the file mentioned in the message and
      provide a summary of its contents.

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
  capabilities:
    default_policy: auto

dev:
  variables:
    workspace: "{{env.PWD}}"
```

> **Background mode requires at least one trigger.** The agent
> is activated each time a trigger fires with the trigger's
> `message` as input. For multi-user routing
> (`broadcast` / `user` / `session`), see
> [Background Sessions](38-background-sessions.md).

## 10 · Parallel execution + background tasks

A polyvalent assistant showing the auto-injected execution
primitives (`run_parallel`, `background_run`) - no YAML
config needed for those.

```yaml
app:
  app_id: smart-chat
  name: Smart Chat

runtime:
  mode: conversation
  max_turns: 200
  timeout: 1200
  workdir: "{{env.PWD}}"

agents:
  - id: assistant
    role: assistant
    brain:
      provider: openai
      model: gpt-4o-mini
      backend: openai_compat
      config: {api_key: "{{secret.OPENAI_API_KEY}}"}
      context:
        max_tokens: 128000
        output_reserved: 2000
        strategy: summarize
        keep_recent: 20
        compression_trigger: 0.85
        auto_compact: true
    system_prompt: |
      Tu disposes de primitives d'exécution :
        - run_parallel : actions indépendantes en parallèle
        - background_run : tâches longues non bloquantes
      Utilise run_parallel pour les actions indépendantes.
      Utilise background_run pour les téléchargements et opérations lentes.

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep, write]
    shell:
      constraints:
        allowed_actions: [bash]
    http:
      constraints:
        allowed_actions: [get, post, json_api, fetch_page, head, download,
                          download_status, download_cancel, download_list]
    database:
      setup:
        - action: connect
          params:
            connection_id: test_db
            driver: sqlite
            database: "{{workspace}}/test.db"
            policy: {preset: safe_write}
      constraints:
        allowed_actions: [connect, disconnect, execute_query, fetch_results,
                          list_tables, describe]
  capabilities:
    default_policy: approve

ui:
  greeting: "Assistant polyvalent : exécution parallèle + tâches background."

dev:
  variables:
    workspace: "{{env.PWD}}"
```

**Key takeaways:**

- **4 modules** loaded together (filesystem, shell, http, database).
- **Execution primitives** (`run_parallel`, `background_run`,
  `background_status`, `background_result`, `background_cancel`,
  `background_list`, `background_wait`) need no YAML config -
  the `context_builder` injects them automatically.
- `default_policy: approve` makes every tool call ask for
  confirmation by default.

## 11 · Monitoring bot - watchers + scheduler + channels

A fully autonomous monitoring agent. Demonstrates
`runtime.watchers`, `runtime.scheduler`, `runtime.default_channel`,
and the `tools.channels:` block.

```yaml
app:
  app_id: monitoring-bot
  name: Monitoring Bot

runtime:
  mode: background
  entry_agent: monitor
  watchers: true
  scheduler: true
  default_channel: slack_alerts
  max_turns: 200
  timeout: 3600
  workdir: "{{env.PWD}}"
  triggers:
    - id: cron_health
      type: cron
      schedule: "*/5 * * * *"
      message: "Run a health check on all monitored endpoints."
      routing: broadcast

agents:
  - id: monitor
    role: assistant
    brain:
      provider: openai
      model: gpt-4o-mini
      backend: openai_compat
      config: {api_key: "{{secret.OPENAI_API_KEY}}"}
      context:
        max_tokens: 128000
        strategy: summarize
        keep_recent: 20
        compression_trigger: 0.85
        auto_compact: true
    system_prompt: |
      Tu es un bot de monitoring autonome. Tu peux :
        - watch_start : surveiller des endpoints HTTP
        - schedule_once / schedule_cron : planifier des vérifications
        - remember : noter une tâche pour plus tard
        - output_channel="slack_alerts" pour alertes critiques
        - output_channel="audit" pour le journal
      Par défaut, les résultats vont dans la conversation.

tools:
  modules:
    http:
      constraints:
        allowed_actions: [get, head, json_api, fetch_page]
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
  capabilities:
    default_policy: auto

  channels:                              # ← lives under tools:, NOT top-level
    slack_alerts:
      type: webhook
      config:
        url: "{{secret.SLACK_WEBHOOK_URL}}"
        payload_template: |
          {"text": "{{event.message}}", "channel": "#production-alerts"}

    audit:
      type: log
      config:
        logger_name: digitorn.audit
        level: INFO
        format: json
        include_data: true

ui:
  greeting: |
    Monitoring bot prêt. Je peux surveiller, planifier, alerter Slack ou journaliser.
    Que veux-tu surveiller ?
```

**Key takeaways:**

- **`runtime.watchers: true`** + **`runtime.scheduler: true`**
  enable the persistent watcher + scheduler tools.
- **`runtime.default_channel: slack_alerts`** routes every
  watcher / scheduled job to that instance unless the call
  overrides with `output_channel`.
- **`tools.channels:`** declares channel instances. Two
  built-in types in this example: `webhook` (Slack) and `log`
  (structured audit log). Full channel reference in
  [Channels](40-channels.md).
- **Notification buffering** - when no client is connected,
  notifications are buffered in the KV store (max 100, TTL
  24 h) and delivered on reconnect.

## 12 · MCP - multiple external servers

An agent connected to three MCP servers (Slack, GitHub, Brave
Search). Each server declares its OS sandbox permissions.

```yaml
app:
  app_id: mcp-multi
  name: MCP Multi-Server Agent

runtime:
  mode: conversation
  max_turns: 200

agents:
  - id: assistant
    role: assistant
    brain:
      provider: openai
      model: gpt-4o
      backend: openai_compat
      config: {api_key: "{{secret.OPENAI_API_KEY}}"}
      context:
        max_tokens: 128000
        strategy: summarize
        keep_recent: 20
        auto_compact: true
    system_prompt: |
      Tu es connecté à Slack, GitHub et Brave Search via MCP.
      Utilise SearchTools pour découvrir les outils disponibles.

tools:
  modules:
    filesystem:
      constraints:
        allowed_actions: [read, glob, grep]
    mcp:
      config:
        servers:
          slack:
            transport: stdio
            command: npx
            args: ["-y", "@anthropic/mcp-server-slack"]
            env:
              SLACK_TOKEN: "{{secret.SLACK_BOT_TOKEN}}"
            sandbox:
              permissions: [process.exec, net.http]
              allowed_hosts: [slack.com]

          github:
            transport: stdio
            command: npx
            args: ["-y", "@anthropic/mcp-server-github"]
            env:
              GITHUB_TOKEN: "{{secret.GITHUB_PAT}}"
            sandbox:
              permissions: [process.exec, net.http]
              allowed_hosts: [api.github.com]

          brave:
            transport: stdio
            command: npx
            args: ["-y", "@anthropic/mcp-server-brave"]
            env:
              BRAVE_API_KEY: "{{secret.BRAVE_API_KEY}}"
            sandbox:
              permissions: [process.exec, net.http]
              allowed_hosts: [api.search.brave.com]

  capabilities:
    grant:
      - {module: mcp_slack, actions: [list_channels, post_message, search_messages]}
      - {module: mcp_brave, actions: [search]}
      - {module: filesystem, actions: [read, glob, grep]}
    approve:
      - {module: mcp_github, actions: [create_issue, create_pull_request]}
    deny:
      - {module: mcp_github, actions: [delete_repository]}

ui:
  greeting: "3 serveurs MCP connectés : Slack, GitHub, Brave."
```

**Key takeaways:**

- Each MCP server gets its own `mcp_<id>` module namespace
  (Slack tools → `mcp_slack.<action>`).
- **Per-server sandbox** with `permissions` + `allowed_hosts`
  is mandatory in production
  ([Sandbox → MCP servers](35-sandbox.md#mcp-servers---deny-by-default)).
- Slack tools auto-execute, GitHub tools require approval,
  `delete_repository` is hard-blocked.
- `SearchTools` (the discovery meta-tool) finds MCP tools the
  same way it finds native ones.

## 13 · MCP with OAuth2 - Google Calendar (SSE transport)

OAuth2 with PKCE for per-user authorization. SSE transport
injects the bearer token via HTTP header.

```yaml
app:
  app_id: mcp-oauth-demo
  name: Calendar Assistant

runtime:
  mode: conversation

agents:
  - id: assistant
    role: assistant
    brain:
      provider: openai
      model: gpt-4o
      backend: openai_compat
      config: {api_key: "{{secret.OPENAI_API_KEY}}"}
    system_prompt: |
      Tu as accès au Google Calendar et à Slack.
      Si un outil requiert une autorisation OAuth, présente le lien.

tools:
  modules:
    mcp:
      config:
        servers:
          google_calendar:
            transport: sse
            url: http://localhost:3000/sse
            auth:
              type: oauth2
              provider: google
              client_id: "{{secret.GOOGLE_CLIENT_ID}}"
              client_secret: "{{secret.GOOGLE_CLIENT_SECRET}}"
              scopes:
                - https://www.googleapis.com/auth/calendar.readonly
                - https://www.googleapis.com/auth/calendar.events
            sandbox:
              permissions: [net.http]
              allowed_hosts: [www.googleapis.com, oauth2.googleapis.com]

          slack:
            transport: stdio
            command: npx
            args: ["-y", "@anthropic/mcp-server-slack"]
            env:
              SLACK_TOKEN: "{{secret.SLACK_BOT_TOKEN}}"
            sandbox:
              permissions: [process.exec, net.http]
              allowed_hosts: [slack.com]

  capabilities:
    grant:
      - {module: mcp_google_calendar, actions: [list_events, get_event, create_event]}
      - {module: mcp_slack,           actions: [list_channels, post_message]}

ui:
  greeting: "Assistant Calendar + Slack prêt."
```

**Key takeaways:**

- **OAuth2 + PKCE** for SSE / HTTP transports - token sent in
  `Authorization: Bearer ...` header.
- **Mixed auth models** - Google = OAuth2, Slack = static bot
  token via the credentials vault.
- **Auto-refresh** - the OAuth refresh loop renews tokens
  within 10 min of expiry
  ([credentials.md](../reference/runtime/credentials.md)).
- **`requires_oauth` flow** - when the user hasn't yet
  authorised, the tool result carries an `auth_url` the agent
  surfaces.

## 14 · MCP with OAuth2 - Notion (stdio + env_token_var)

stdio transport injects the OAuth token as an environment
variable and **restarts the subprocess** when the token
refreshes.

```yaml
app:
  app_id: notion-agent
  name: Notion Agent

runtime:
  mode: conversation
  max_turns: 200
  timeout: 1200
  workdir: "{{env.PWD}}"

agents:
  - id: assistant
    role: assistant
    brain:
      provider: openai
      model: gpt-4o-mini
      backend: openai_compat
      config: {api_key: "{{secret.OPENAI_API_KEY}}"}
      context:
        max_tokens: 128000
        strategy: summarize
        keep_recent: 20
        auto_compact: true
    system_prompt: |
      Tu es connecté au workspace Notion de l'utilisateur.
      Tu peux rechercher, lire et modifier ses pages et bases de données.

tools:
  modules:
    mcp:
      config:
        servers:
          notion:
            transport: stdio
            command: mcp-notion
            args: []
            auth:
              type: oauth2
              provider: notion
              client_id: "{{secret.NOTION_CLIENT_ID}}"
              client_secret: "{{secret.NOTION_CLIENT_SECRET}}"
              env_token_var: NOTION_API_KEY      # token injected as env var
              redirect_uri: http://localhost:8913/callback
            sandbox:
              permissions: [process.exec, net.http]
              allowed_hosts: [api.notion.com]
  capabilities:
    default_policy: approve

ui:
  greeting: |
    Agent Notion prêt ! Première connexion : tu dois autoriser l'accès
    à ton workspace Notion (1 clic).
```

**Key takeaways:**

- **`env_token_var: NOTION_API_KEY`** - the field that bridges
  OAuth2 tokens into stdio MCP servers. The daemon restarts
  the subprocess when the token refreshes.
- **Notion provider** - pre-configured in the credentials
  catalog (Basic auth for token exchange, JSON body - not
  form-encoded).
- **Local OAuth callback** - in standalone mode, a temporary
  HTTP server on the configured `redirect_uri` port handles
  the callback and opens the browser automatically.
- **User-scoped sharing** - the user must select the pages /
  databases to share at authorisation time. Skipping that
  step yields an empty workspace; modify later in
  Notion → Settings → My connections.

## Cross-references

- Block-by-block YAML reference:
  [App Configuration](02-app-config.md)
- Agents + brains + sub-agent dispatch (`Agent` tool, 8
  modes): [Agents](03-agents.md)
- Triggers + routing modes:
  [Triggers](09-triggers.md), [Background Sessions](38-background-sessions.md)
- Channels (webhook, log, slack, email, ...) with notification
  buffering: [Channels](40-channels.md)
- Security model + capabilities resolution:
  [Security](11-security.md)
- OS-level sandbox (Landlock + seccomp + namespaces) + MCP
  per-server permissions:
  [OS-Level Sandbox](35-sandbox.md)
- Credentials vault, OAuth flow, KMS:
  [credentials.md](../reference/runtime/credentials.md)
- Behavior engine (declarative rules + classifier):
  [Behavior Engine](43-behavior.md)
- API surface (REST + Socket.IO):
  [API Integration](14-api-integration.md)
