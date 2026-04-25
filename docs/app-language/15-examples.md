---
id: examples
---

# Examples

Complete, real-world application examples demonstrating different features of the Digitorn App Language. 13 examples covering basic chat, context management, security, multi-agent, background mode, execution primitives, watchers, scheduler, output channels, MCP server integration, and MCP with OAuth2.

## 1. Minimal Chat

The simplest possible app — an LLM with a greeting module.

```yaml
app:
  app_id: chat-assistant
  name: "Chat Assistant"
  description: "Interactive conversation with tool access."

modules:
  filesystem: {}

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      Tu es un assistant intelligent et amical. Tu reponds en francais.
      Tu as acces a des outils — utilise-les quand c'est pertinent.

execution:
  mode: conversation
  greeting: "Bienvenue ! Je suis ton assistant Digitorn."

capabilities:
  default_policy: auto
```
## 2. One-Shot Task

Process a single input and return.

```yaml
app:
  app_id: hello-oneshot
  name: "Hello One-Shot"
  description: "A simple one-shot app that responds to messages."

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You are a friendly assistant. Be concise and helpful.
      You have access to tools — use them when relevant.

execution:
  mode: one_shot
  input:
    type: text
    description: "A question or message"
  output:
    type: text

capabilities:
  default_policy: auto
```
Run it:

```bash
digitorn run hello-oneshot.yaml "Say hello in 3 languages"
```

## 3. Smart Chat with Context Management

Conversation mode with automatic context compaction using the `summarize` strategy and a dedicated summary brain.

```yaml
app:
  app_id: smart-chat
  name: "Smart Chat"
  description: "Assistant avec gestion automatique du contexte et outils fichiers."

variables:
  workspace: "{{env.PWD}}"

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep, write]

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
      context:
        max_tokens: 80000
        output_reserved: 1000
        strategy: summarize
        keep_recent: 20
        compression_trigger: 0.90
        summary_max_tokens: 5120
        auto_compact: true
    system_prompt: |
      Tu es un assistant intelligent. Tu reponds en francais.
      Tu as acces a des outils via un systeme de decouverte.

      WORKFLOW EFFICACE pour utiliser un outil :
      1. list_categories -> voir les modules disponibles
      2. browse_category(category="nom") -> voir les outils d'un module
      3. execute_tool(name="module.action", params={...}) -> executer

      IMPORTANT :
      - N'appelle PAS get_tool sauf si tu as besoin de la doc detaillee.
      - Va directement a execute_tool des que tu connais le nom de l'outil.
      - Limite-toi a 3-5 appels d'outils maximum par question.
      - Si un outil echoue, explique l'erreur au lieu de reessayer en boucle.

execution:
  mode: conversation
  greeting: "Salut ! Je suis un assistant avec gestion automatique du contexte."
  max_turns: 40
  timeout: 1200.0

capabilities:
  default_policy: auto
```
## 4. Local LLM with Ollama

Using a local model without native tool calling — tools are injected in the system prompt.

```yaml
app:
  app_id: ollama-chat
  name: "Ollama Chat (text-based tools)"
  description: "Local LLM without native tool use — tools injected in prompt."

variables:
  workspace: "{{env.PWD}}"

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

agents:
  - id: assistant
    role: assistant
    brain:
      provider: ollama
      model: qwen2.5:14b-instruct-q4_K_M
      backend: openai_compat
      config:
        base_url: "http://localhost:11434/v1"
      context:
        max_tokens: 8000
        output_reserved: 1000
        strategy: truncate
        keep_recent: 6
        compression_trigger: 0.60
        auto_compact: true
    system_prompt: |
      Tu es un assistant intelligent. Tu reponds en francais.
      Tu as acces a des outils via un systeme de decouverte.

      WORKFLOW EFFICACE pour utiliser un outil :
      1. list_categories -> voir les modules disponibles
      2. browse_category(category="nom") -> voir les outils d'un module
      3. execute_tool(name="module.action", params={...}) -> executer

      IMPORTANT :
      - Va directement a execute_tool des que tu connais le nom de l'outil.
      - Limite-toi a 3-5 appels d'outils maximum par question.
      - Si un outil echoue, explique l'erreur au lieu de reessayer en boucle.

execution:
  mode: conversation
  greeting: "Salut ! Je suis un assistant local avec des outils injectes dans le prompt."
  max_turns: 10
  timeout: 300.0

capabilities:
  default_policy: auto
```
> **Note**: The `timeout: 300.0` is important for local models — they're slower than cloud APIs.

> **Tip**: If your model supports native tool calling (e.g., `qwen2.5-coder`), add `native_tool_use: true` to the brain config for significantly better tool calling reliability.

```yaml
brain:
  provider: ollama
  model: qwen2.5-coder:7b
  native_tool_use: true
```
## 5. Context Management Test

Test context compaction by setting an aggressive trigger threshold.

```yaml
app:
  app_id: context-test
  name: "Context Management Test"
  description: "Test automatic context compaction with notifications."

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
      context:
        max_tokens: 0
        output_reserved: 4096
        strategy: summarize
        keep_recent: 6
        compression_trigger: 0.15  # Very low — compaction after a few exchanges
        summary_max_tokens: 512
        auto_compact: true
    system_prompt: |
      Tu es un assistant de test. Reponds en francais.
      Sois detaille dans tes reponses pour generer du contenu.

execution:
  mode: conversation
  greeting: "Test de gestion du contexte. La compaction se declenchera rapidement."
  max_turns: 50
  timeout: 120.0
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
```
## 6. Smart Chat with Summary Brain

Same as Smart Chat, but uses a cheap local model for context summarization instead of the main cloud model.

```yaml
app:
  app_id: smart-chat-local-summary
  name: "Smart Chat (Local Summary)"
  description: "Cloud model for chat, local model for compaction."

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

agents:
  - id: assistant
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
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
    system_prompt: |
      Tu es un assistant intelligent. Tu reponds en francais.

execution:
  mode: conversation
  greeting: "Assistant avec compaction locale."
  max_turns: 40
  timeout: 1200.0

capabilities:
  default_policy: auto
```
> **Note**: The `summary_brain` uses a small local model (3B params) for summarization. This avoids spending cloud API tokens on compaction while keeping the main conversation on a powerful model.

## 7. Secure Read-Only Analyst

An app restricted to read-only operations with explicit security.

```yaml
app:
  app_id: secure-analyst
  name: "Secure Analyst"
  description: "Read-only data analysis assistant."

variables:
  workspace: "{{env.PWD}}"

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

agents:
  - id: analyst
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You are a data analyst. You have read-only access.
      Never attempt to modify files or execute write queries.

execution:
  mode: conversation
  greeting: "Data analyst ready. I can read files and query databases."

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
## 8. Multi-Agent App

Two agents with different providers — a coordinator and a worker.

```yaml
app:
  app_id: multi-agent
  name: "Multi-Agent"
  description: "Coordinator + worker with different providers."

variables:
  workspace: "{{env.PWD}}"

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
      context:
        strategy: summarize
        keep_recent: 20
        compression_trigger: 0.70
    system_prompt: |
      You orchestrate tasks and delegate to workers.

  - id: worker
    role: worker
    brain:
      provider: groq
      model: llama-3.3-70b-versatile
      backend: openai_compat
      config:
        api_key: "{{env.GROQ_API_KEY}}"
        base_url: "https://api.groq.com/openai/v1"
      context:
        strategy: truncate
        keep_recent: 10
    system_prompt: |
      You execute tasks assigned by the coordinator.

execution:
  mode: conversation
  entry_agent: coordinator
  greeting: "Multi-agent system ready."
  max_turns: 30

capabilities:
  default_policy: auto
```
## 9. Background Mode with Triggers

A daemon app that watches for new CSV files and processes them automatically.

```yaml
app:
  app_id: csv-watcher
  name: "CSV Watcher"
  description: "Watches inbox for new CSV files and analyzes them."

variables:
  workspace: "{{env.PWD}}"

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

agents:
  - id: analyst
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    system_prompt: |
      You are a data analyst. When activated, read the file mentioned
      in the message and provide a summary of its contents.

execution:
  mode: background
  max_turns: 10
  timeout: 60.0
  triggers:
    # Watch for new CSV files
    - id: new_csv
      type: watch
      paths: ["{{workspace}}/inbox/*.csv"]
      message: "New CSV file detected: {{event.path}}. Please analyze it."

    # Hourly summary
    - id: hourly_report
      type: cron
      schedule: "0 * * * *"
      message: "Generate an hourly summary of all files in {{workspace}}/inbox/."

capabilities:
  default_policy: auto
```
> **Note**: Background mode requires at least one trigger. The agent is activated each time a trigger fires, with the trigger's `message` as input.

## 10. Parallel Execution & Background Tasks

A polyvalent assistant showcasing execution primitives — parallel actions across modules and non-blocking background tasks.

```yaml
app:
  app_id: smart-chat
  name: "Smart Chat"
  description: "Assistant with parallel execution and background tasks."

variables:
  workspace: "{{env.PWD}}"

modules:
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep, write]
  shell:
    constraints:
      allowed_actions: [bash]
  http:
    constraints:
      allowed_actions: [get, post, json_api, fetch_page, head, download, download_status, download_cancel, download_list]
  database:
    setup:
      - action: connect
        params:
          connection_id: test_db
          driver: sqlite
          database: "{{workspace}}/test.db"
          policy:
            preset: safe_write
    constraints:
      allowed_actions: [connect, disconnect, execute_query, fetch_results, list_tables, describe]

agents:
  - id: assistant
    role: assistant
    brain:
      provider: openai
      model: "gpt-4o-mini"
      backend: openai_compat
      config:
        api_key: "{{env.OPENAI_API_KEY}}"
      context:
        max_tokens: 128000
        output_reserved: 2000
        strategy: summarize
        keep_recent: 20
        compression_trigger: 0.85
        auto_compact: true
    system_prompt: |
      Tu es un assistant intelligent et polyvalent. Tu reponds en francais.

      Tu as acces a des primitives d'execution puissantes :
      - **run_parallel** : execute plusieurs actions en meme temps
      - **background_run** : lance une tache longue en arriere-plan

      Utilise run_parallel quand tu as des actions independantes.
      Utilise background_run pour les telechargements ou operations longues.

execution:
  mode: conversation
  greeting: "Salut ! Assistant polyvalent avec execution parallele et taches background."
  max_turns: 200
  timeout: 1200.0

capabilities:
  default_policy: approve
```
**Key features demonstrated:**
- **4 modules** loaded: filesystem, shell, http, database
- **Execution primitives** (always available, no config needed):
  - `run_parallel` — batch independent actions across any module
  - `background_run/status/result/cancel/list/wait` — non-blocking long tasks
- **Approval policy** — user confirms before actions execute

> **Note**: Execution primitives (`run_parallel`, `background_*`) require no YAML configuration — they are automatically injected by the `context_builder` for every agent. See [Execution Primitives](04c-primitives.md) for details.

## 11. Monitoring Bot with Scheduler & Output Channels

A fully autonomous monitoring agent that watches HTTP endpoints, schedules health checks, and routes alerts to Slack via webhooks. Demonstrates watchers, scheduler, output channels, and notification buffering.

```yaml
app:
  app_id: monitoring-bot
  name: "Monitoring Bot"
  description: "Autonomous monitoring with scheduled checks and multi-channel alerts."

variables:
  workspace: "{{env.PWD}}"

channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{env.SLACK_WEBHOOK_URL}}"
      payload_template: |
        {"text": "{{event.message}}", "channel": "#production-alerts"}

  audit:
    type: log
    config:
      logger_name: "digitorn.audit"
      level: INFO
      format: json
      include_data: true

modules:
  http:
    constraints:
      allowed_actions: [get, head, json_api, fetch_page]
  filesystem:
    constraints:
      allowed_actions: [read, glob, grep]

agents:
  - id: monitor
    role: assistant
    brain:
      provider: openai
      model: gpt-4o-mini
      backend: openai_compat
      config:
        api_key: "{{env.OPENAI_API_KEY}}"
      context:
        max_tokens: 128000
        strategy: summarize
        keep_recent: 20
        compression_trigger: 0.85
        auto_compact: true
    system_prompt: |
      Tu es un bot de monitoring autonome. Tu reponds en francais.

      Tu peux :
      - Surveiller des endpoints HTTP avec des watchers (watch_start)
      - Planifier des verifications futures (schedule_once, schedule_cron)
      - Te souvenir de taches a faire (remember)
      - Router les alertes vers Slack (output_channel: "slack_alerts")
        ou vers le log d'audit (output_channel: "audit")

      Par defaut, les resultats vont dans la conversation.
      Utilise slack_alerts pour les alertes critiques.

execution:
  mode: conversation
  watchers: true
  scheduler: true
  default_channel: slack_alerts
  greeting: |
    Monitoring bot ready. I can:
    - Watch HTTP endpoints and alert on errors
    - Schedule periodic health checks
    - Route alerts to Slack or internal audit log
    - Remember to do things later
    What would you like to monitor?
  max_turns: 200
  timeout: 3600.0

capabilities:
  default_policy: auto
```
**Key features demonstrated:**

- **Output channels** — `slack_alerts` (webhook) and `audit` (structured logging)
- **Watchers** (`execution.watchers: true`) — persistent HTTP endpoint monitoring with smart escalation
- **Scheduler** (`execution.scheduler: true`) — one-shot timers, cron jobs, and `remember` shortcuts
- **Default channel** — all jobs/watchers route to `slack_alerts` unless overridden
- **Notification buffering** — if no session is connected, notifications are buffered in KV (max 100, TTL 24h) and delivered on reconnect

Example conversation:

```text
User: "Watch https://api.example.com/health every 30 seconds, alert me on errors"
Agent: [calls watch_start(name="http.get", params={url: "..."}, interval=30, notify_when="on_error")]
       -- Watcher started! Checking every 30s, I'll alert you if it goes down.

User: "Schedule a full API test tomorrow at 9am, send results to Slack"
Agent: [calls schedule_once(when="tomorrow at 9am", action_type="tool_call",
        tool_name="http.get", tool_params={url: "..."}, output_channel="slack_alerts")]
       -- Scheduled! The health check will run tomorrow at 09:00 and post to #production-alerts.

User: "Remind me to check the deployment logs in 2 hours"
Agent: [calls remember(what="Check deployment logs", when="in 2h")]
       -- Got it! I'll remind you in 2 hours.
```

> **Note**: The `channels:` block defines where notifications go. The `execution.default_channel` sets the default for all watchers and scheduled jobs. Individual jobs can override with `output_channel`. See [Output Channels](05-channels.md) for the full channel system documentation.

## 12. MCP Agent with External Servers

An agent connected to external MCP servers (Slack, GitHub, Brave Search). Tools from MCP servers are auto-indexed and discoverable like native tools, with fine-grained security per server.

```yaml
app:
  app_id: mcp-multi
  name: "MCP Multi-Server Agent"
  description: "Agent connecte a Slack, GitHub et Brave Search via MCP."

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
            SLACK_TOKEN: "{{env.SLACK_TOKEN}}"
          sandbox: { permissions: [process.exec, net.http] }
        github:
          transport: stdio
          command: npx
          args: ["-y", "@anthropic/mcp-server-github"]
          env:
            GITHUB_TOKEN: "{{env.GITHUB_TOKEN}}"
          sandbox: { permissions: [process.exec, net.http] }
        brave:
          transport: stdio
          command: npx
          args: ["-y", "@anthropic/mcp-server-brave"]
          env:
            BRAVE_API_KEY: "{{env.BRAVE_API_KEY}}"
          sandbox: { permissions: [process.exec, net.http] }

agents:
  - id: assistant
    role: assistant
    brain:
      provider: openai
      model: gpt-4o
      backend: openai_compat
      config:
        api_key: "{{env.OPENAI_API_KEY}}"
      context:
        max_tokens: 128000
        strategy: summarize
        keep_recent: 20
        auto_compact: true
    system_prompt: |
      Tu es un assistant connecte a Slack, GitHub et Brave Search.
      Utilise search_tools pour trouver les outils MCP disponibles.

execution:
  mode: conversation
  greeting: |
    3 serveurs MCP connectes : Slack, GitHub, Brave Search.
    Leurs outils sont disponibles comme des outils natifs.
  max_turns: 200

capabilities:
  grant:
    - module: mcp_slack
      actions: [list_channels, post_message, search_messages]
    - module: mcp_brave
      actions: [search]
    - module: filesystem
      actions: [read, glob, grep]
  approve:
    - module: mcp_github
      actions: [create_issue, create_pull_request]
  deny:
    - module: mcp_github
      actions: [delete_repository]
```
**Key features demonstrated:**

- **MCP module** with 3 external servers (Slack, GitHub, Brave) connected via stdio transport
- **Per-server security** — Slack tools auto-execute, GitHub requires approval, delete_repository is blocked
- **Native + MCP coexistence** — filesystem (native) + MCP servers in the same app
- **Auto-indexing** — MCP tools appear in `list_categories` as `mcp_slack`, `mcp_github`, `mcp_brave`

Example conversation:

```text
User: "Search for recent Anthropic news"
Agent: [calls search_tools(query="search web")]
       [calls execute_tool(name="mcp_brave.search", params={"query": "Anthropic news 2026"})]
       -> Here are the latest results about Anthropic...

User: "Post a summary to #engineering on Slack"
Agent: [calls execute_tool(name="mcp_slack.post_message", params={"channel": "#engineering", "text": "..."})]
       -> Message posted to #engineering!

User: "Create a GitHub issue to track this"
Agent: [calls execute_tool(name="mcp_github.create_issue", params={...})]
       -> [Approval prompt: mcp_github.create_issue — approve?]
       -> Issue created: #42 "Track Anthropic updates"
```

> **Note**: MCP tools are auto-discovered via `search_tools` and `browse_category`. The agent doesn't need to know which tools come from MCP vs native modules — the discovery workflow is identical. See [MCP Servers](04d-mcp.md) for the full MCP documentation.

---

## 13. MCP with OAuth2 — Google Calendar

An assistant that accesses Google Calendar via MCP with per-user OAuth2 authentication.

```yaml
app:
  app_id: mcp-oauth-demo
  name: "Calendar Assistant"
  description: "MCP agent with OAuth2 user-level auth for Google Calendar."

modules:
  mcp:
    config:
      servers:
        google_calendar:
          transport: sse
          url: "http://localhost:3000/sse"
          auth:
            type: oauth2
            provider: google
            client_id: "{{secret.GOOGLE_CLIENT_ID}}"
            client_secret: "{{secret.GOOGLE_CLIENT_SECRET}}"
            scopes:
              - "https://www.googleapis.com/auth/calendar.readonly"
              - "https://www.googleapis.com/auth/calendar.events"
          sandbox: { permissions: [net.http] }
        slack:
          transport: stdio
          command: npx
          args: ["-y", "@anthropic/mcp-server-slack"]
          env:
            SLACK_TOKEN: "{{env.SLACK_TOKEN}}"
          sandbox: { permissions: [process.exec, net.http] }

agents:
  - id: assistant
    role: assistant
    brain:
      provider: openai
      model: gpt-4o
      backend: openai_compat
      config:
        api_key: "{{env.OPENAI_API_KEY}}"
    system_prompt: |
      Tu es un assistant avec acces au Google Calendar et Slack.
      Si un outil requiert une autorisation OAuth, presente le lien a l'utilisateur.

execution:
  mode: conversation
  greeting: "Assistant Calendar + Slack pret."

capabilities:
  grant:
    - module: mcp_google_calendar
      actions: [list_events, get_event, create_event]
    - module: mcp_slack
      actions: [list_channels, post_message]
```
**Key features demonstrated:**

- **OAuth2 with PKCE** — Google Calendar requires per-user authorization
- **Mixed auth models** — Google Calendar uses OAuth2, Slack uses a static bot token
- **Transparent token refresh** — tokens are auto-refreshed 5 minutes before expiry
- **`requires_oauth` flow** — if user hasn't authorized, agent receives an `auth_url` to present

Example conversation:

```text
User: "What's on my calendar today?"
Agent: [calls execute_tool(name="mcp_google_calendar.list_events", params={...})]
       -> Error: User needs to authorize Google Calendar.
       Please open this link to authorize: https://accounts.google.com/o/oauth2/v2/auth?...

User: [opens link, authorizes, returns]
User: "Try again"
Agent: [calls execute_tool(name="mcp_google_calendar.list_events", params={...})]
       -> You have 3 meetings today:
         - 10:00 Team standup
         - 14:00 Design review
         - 16:30 1:1 with Alice

User: "Post the schedule to #team on Slack"
Agent: [calls execute_tool(name="mcp_slack.post_message", params={"channel": "#team", "text": "..."})]
       -> Schedule posted to #team!
```

> **Note**: OAuth tokens are stored encrypted (Fernet) and scoped per-user. Each user authorizes independently. See [MCP OAuth2](04d-mcp.md) for the full OAuth documentation.

---

## 14. MCP with OAuth2 — Notion (stdio + env_token_var)

An assistant connected to Notion via MCP with OAuth2 authentication. Unlike SSE/HTTP transports where the token is injected via HTTP headers, stdio transports inject the token as an environment variable and restart the subprocess.

```yaml
app:
  app_id: notion-agent
  name: "Notion Agent"
  description: "Assistant connecte a Notion via OAuth — l'utilisateur autorise son workspace en 1 clic."

variables:
  workspace: "{{env.PWD}}"

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
            client_id: "{{secret.NOTION_OAUTH_CLIENT_ID}}"
            client_secret: "{{secret.NOTION_OAUTH_CLIENT_SECRET}}"
            env_token_var: "NOTION_API_KEY"
            redirect_uri: "http://localhost:8913/callback"
          sandbox: { permissions: [process.exec, net.http] }

agents:
  - id: assistant
    role: assistant
    brain:
      provider: openai
      model: "gpt-4o-mini"
      backend: openai_compat
      config:
        api_key: "{{env.OPENAI_API_KEY}}"
      context:
        max_tokens: 128000
        strategy: summarize
        keep_recent: 20
        auto_compact: true
    system_prompt: |
      Tu es un assistant connecte au workspace Notion de l'utilisateur.
      Tu peux rechercher, lire et modifier ses pages et bases de donnees.
      Reponds toujours en francais.

execution:
  mode: conversation
  greeting: |
    Agent Notion pret !
    Si c'est ta premiere connexion, je te demanderai d'autoriser l'acces
    a ton workspace Notion (1 clic).
  max_turns: 200
  timeout: 1200.0

capabilities:
  default_policy: approve
```
**Key features demonstrated:**

- **OAuth2 for stdio transport** — token injected as `NOTION_API_KEY` env var, subprocess restarted automatically
- **`env_token_var`** — the critical field that bridges OAuth2 tokens to stdio MCP servers
- **Notion provider** — pre-configured URLs, Basic auth for token exchange, JSON body (not form-encoded)
- **Local OAuth flow** — in standalone mode, a temporary HTTP server on port 8913 handles the callback and opens the browser automatically
- **`buffer_size`** — Notion returns large JSON responses; the default 10 MB buffer handles most workspaces (increase with `buffer_size` if needed)

Example conversation:

```text
[Browser opens: Notion authorization page]
User: [selects pages to share, clicks "Allow access"]
-> Authorization successful!

User: "Liste les bases de donnees dans mon Notion"
Agent: [calls mcp_notion.search_notion(filter_type="database")]
       -> Voici vos bases de donnees :
         - Projets (12 entries)
         - Contacts (45 entries)
         - Taches (89 entries)

User: "Montre-moi les taches en cours"
Agent: [calls mcp_notion.query_database(database_id="...", filter={"status": "En cours"})]
       -> 5 taches en cours :
         - Refactoring API (deadline: 15 mars)
         - Design review (deadline: 12 mars)
         ...
```

> **Note**: When authorizing via Notion OAuth, you must **select the pages and databases** to share with the integration. If you skip this step, the integration will see an empty workspace. You can modify shared pages later in Notion Settings > My connections.
