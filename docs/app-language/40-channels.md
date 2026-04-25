---
sidebar_position: 40
title: Channels — Unified Bidirectional I/O
---

# Channels Module

The `channels` module is Digitorn's **unified bidirectional I/O system**. It receives events from any external source (webhooks, cron schedules, email, file changes, RSS feeds, message queues) and responds through the same or a different channel — all configured in YAML, with no code required.

This module replaces the separate `execution.triggers` and top-level `channels:` systems with a single, composable, security-first design.

## Why Channels?

In real-world applications, input and output use the same pipe:

```
WhatsApp message arrives  →  agent processes  →  reply on WhatsApp
Email arrives             →  agent processes  →  reply by email
GLPI webhook fires        →  agent processes  →  notify on Slack
Cron schedule fires       →  agent processes  →  send report by email
```

The `channels` module models this as **bidirectional providers** — each provider can listen for events (inbound), send messages (outbound), or both.

---

## Installation

The channels module is built-in. Add it to your app's `modules:` block:

```yaml
modules:
  channels:
    config:
      providers:
        # ... your providers here
```
No pip install needed. The module is auto-discovered by the Digitorn loader via its `digitorn-module.toml` manifest.

### Optional Dependencies

Some adapters require optional packages:

| Adapter | Direction | Optional dependency | Install |
|---------|-----------|-------------------|---------|
| cron | Inbound | None | Built-in |
| file_watcher | Inbound | None | Built-in |
| webhook | Both | aiohttp (outbound) | `pip install aiohttp` |
| email | Both | None (stdlib imaplib/smtplib) | Built-in |
| telegram | Both | aiohttp | `pip install aiohttp` |
| discord | Both | aiohttp | `pip install aiohttp` |
| slack | Both | aiohttp | `pip install aiohttp` |
| voice | Both | aiohttp | `pip install aiohttp` |
| rss | Inbound | feedparser | `pip install feedparser` |
| log | Outbound | None | Built-in |
| queue | Both | None | Built-in |

---

## Quick Start

### Minimal: Cron trigger

```yaml
app:
  app_id: daily-bot
  name: Daily Bot

modules:
  channels:
    config:
      providers:
        morning_check:
          adapter: cron
          config:
            schedule: "0 9 * * 1-5"
          activation:
            message: "Good morning. Run the daily status check."

agents:
  - id: main
    brain: { provider: openai, model: gpt-4o }
    role: "Check systems and report status."

execution:
  mode: background
```
### Bidirectional: Webhook in + Slack out

```yaml
modules:
  channels:
    config:
      providers:
        github:
          adapter: webhook
          config:
            inbound_path: "/hook/github"
            auth: signature
            signature_secret: "{{secret.GH_WEBHOOK_SECRET}}"
            signature_header: "X-Hub-Signature-256"
          activation:
            message: "GitHub event: {{event.payload.action}} on {{event.payload.repository.full_name}}"

        slack:
          adapter: webhook
          config:
            url: "{{secret.SLACK_WEBHOOK_URL}}"
```
The agent receives GitHub events and can notify on Slack:
```
Agent calls: channels.send_message(provider="slack", text="PR #42 merged!")
```

---

## Core Concepts

### Provider

A named instance of a channel adapter, configured in YAML. Each provider has:

- **adapter** — the transport type (`webhook`, `cron`, `email`, `file_watcher`, `rss`, `log`, `queue`)
- **config** — adapter-specific settings (URLs, credentials, schedules, paths)
- **activation** — how inbound events start the agent (filtering, enrichment, routing, session, reply mode)

### Adapter

The underlying transport implementation. Each adapter is a Python class extending `BaseChannelAdapter`.

| Direction | Adapters |
|-----------|----------|
| **Inbound only** | `cron`, `file_watcher`, `rss` |
| **Outbound only** | `log` |
| **Bidirectional** | `webhook`, `email`, `queue` |

### Activation Pipeline

When an inbound event arrives, it flows through a configurable pipeline:

```
Inbound Event
    │
    ▼
┌─────────┐   Drop events not matching conditions
│ Filter  │   (equals, not_equals, contains, gt, lt)
└────┬────┘
     ▼
┌─────────┐   Call tools via ServiceBus BEFORE agent starts
│ Prepare │   (database lookups, RAG queries, API calls)
└────┬────┘
     ▼
┌─────────┐   Pick the right agent based on enriched data
│  Route  │   (static or dynamic field matching)
└────┬────┘
     ▼
┌─────────┐   Construct system prompt + user message
│  Build  │   from templates with {{variable}} substitution
└────┬────┘
     ▼
┌─────────┐   Run agent_turn() with full tool access
│  Agent  │   (all modules available: database, filesystem, rag, ...)
└────┬────┘
     ▼
┌─────────┐   If reply: auto, send response back
│  Reply  │   on the originating channel
└─────────┘
```

Every step is optional and YAML-configured. The simplest activation has no pipeline at all — just a message template.

### Template Variables

Channels templates support two categories of variables:

**Compile-time** (resolved when the YAML is compiled):

| Variable | Source | Example |
|----------|--------|---------|
| `{{env.VAR}}` | Environment variable | `{{env.SUPPORT_EMAIL}}` |
| `{{secret.VAR}}` | Encrypted secret store | `{{secret.WEBHOOK_SECRET}}` |
| `{{sys.hostname}}` | System info | `prod-server-1` |
| `{{sys.date}}` | Current date | `2026-03-27` |
| `{{sys.timestamp}}` | ISO 8601 UTC time | `2026-03-27T16:39:06+00:00` |
| `{{sys.platform}}` | OS platform | `linux` |
| `{{sys.user}}` | OS username | `paul` |
| `{{app.id}}` | App ID from YAML | `my-support-bot` |
| `{{app.name}}` | App name from YAML | `IT Support Bot` |
| `{{app.version}}` | App version | `2.1` |

See [App Configuration](02-app-config.md#variables) for the full list of `sys.*` and `app.*` variables.

**Runtime** (resolved when an event arrives, in `activation.message`, `activation.context`, `activation.prepare[].params`):

| Variable | Source | Example |
|----------|--------|---------|
| `{{event.source}}` | Sender (phone, email, IP) | `+33612345678` |
| `{{event.payload.field}}` | Raw event data | `{{event.payload.action}}` |
| `{{event.data.field}}` | Alias for payload | `{{event.data.title}}` |
| `{{event.provider}}` | Provider instance name | `whatsapp` |
| `{{event.adapter}}` | Adapter type | `webhook` |
| `{{event.message}}` | Text content (if any) | `Hello, I need help` |
| `{{caller.name}}` | From a prepare step `as: caller` | `Jean Dupont` |
| `{{items.0.title}}` | List index access | First item's title |

**Mixing both in the same template:**

```yaml
activation:
  context: |
    App: {{app.name}} v{{app.version}} on {{sys.hostname}}
    Client: {{caller.name}} ({{caller.plan}})
    Channel: {{event.adapter}} from {{event.source}}
    Deployed: {{sys.date}}
  message: "{{event.payload.message}}"
```
`{{app.name}}`, `{{sys.hostname}}`, and `{{sys.date}}` are resolved at compile time. `{{caller.name}}`, `{{event.adapter}}`, and `{{event.payload.message}}` are preserved and resolved at runtime when an event arrives.

**Security**: `{{secret.*}}` and `{{env.*}}` are resolved at compile time only. Runtime templates use single-pass string substitution (no eval, no Jinja2, no recursion).

---

## Configuration Reference

### Module-Level Config

```yaml
modules:
  channels:
    config:
      # ── Provider instances (the main config) ──
      providers: {}

      # ── Global activation defaults ──
      default_agent: ""              # Agent ID for activations (empty = entry agent)
      max_turns: 30                  # Max LLM turns per activation (1-200)
      timeout: 120.0                 # Timeout per activation in seconds (5-3600)

      # ── History & observability ──
      history_limit: 200             # Max events kept in memory (0-10000)

      # ── Security ──
      secret_filter_enabled: true    # Scan outbound messages for leaked secrets
```
### Provider Config

```yaml
providers:
  my_provider:
    # ── Required ──
    adapter: webhook                 # Adapter type (see Adapters section)

    # ── Adapter-specific ──
    config: {}                       # Passed directly to the adapter

    # ── Activation pipeline ──
    activation:
      agent: ""                      # Target agent (empty = default_agent)
      session: "per_event"           # Session strategy (see below)
      message: ""                    # User message template
      context: ""                    # Extra system prompt context
      expose_data: false             # Write event data to workbench
      reply: "none"                  # Reply mode: "auto", "none", "explicit"
      filter: []                     # Filter conditions (see below)
      prepare: []                    # Pre-activation tool calls (see below)
      route: null                    # Dynamic agent routing (see below)

    # ── Provider settings ──
    enabled: true                    # Enable/disable this provider
    max_concurrent: 5                # Max parallel activations (1-100)
```
### Session Strategies

Controls how conversation history is managed across activations.

| Strategy | YAML value | Behavior |
|----------|-----------|----------|
| **Per-event** | `"per_event"` | Fresh session each time. No memory between activations. Best for webhooks, cron. |
| **Shared** | `"shared"` | Persistent session per (provider + source). Conversation continues across events. Best for WhatsApp, SMS, chat. |
| **Template** | Any `{{...}}` string | Custom session key. `"wa-{{event.source}}"` creates one session per phone number. `"ticket-{{event.payload.id}}"` creates one per ticket. |

**Example: WhatsApp conversation continuity**
```yaml
activation:
  session: "wa-{{event.source}}"   # Same phone = same conversation
  reply: auto
```
The agent remembers the full conversation with each phone number. When a new message arrives from `+33612345678`, the agent sees the complete history.

### Filter Conditions

Drop events before they reach the agent. All conditions use dot-path access into the event data.

```yaml
activation:
  filter:
    # Exact match
    - field: event.payload.status
      equals: "new"

    # Negative match
    - field: event.payload.type
      not_equals: "spam"

    # Substring search
    - field: event.payload.title
      contains: "URGENT"

    # Numeric comparison
    - field: event.payload.amount
      gt: 100

    - field: event.payload.priority
      lt: 3
```
Multiple filters are AND-ed — all must pass for the event to proceed.

### Prepare Steps

Call any module action **before the agent starts**. Results are available in templates as `{{as_field_name.path}}`.

```yaml
activation:
  prepare:
    # Look up the caller in the database
    - action: database.fetch_results
      params:
        query: "SELECT * FROM clients WHERE phone = '{{event.source}}'"
      as: caller

    # Search the knowledge base for relevant procedures
    - action: rag.query
      params:
        knowledge_base: procedures
        query: "procedure for {{event.payload.category}}"
      as: procedure

    # Get recent tickets from GLPI
    - action: database.fetch_results
      params:
        query: "SELECT id,title,status FROM tickets WHERE client_id={{caller.id}} ORDER BY created_at DESC LIMIT 5"
      as: recent_tickets
```
Prepare steps execute sequentially. Each step can reference results from previous steps (`{{caller.id}}` in the third step uses the result from the first).

**Security**: Prepare steps use the ServiceBus — all permissions, rate limits, and audit logging apply. A prepare step calling `database.fetch_results` goes through the same security gates as if the agent called it directly.

### Dynamic Routing

Route events to different agents based on data.

```yaml
activation:
  route:
    field: caller.department       # Dot-path into prepare results or event data
    rules:
      - match: "tech"
        agent: tech_support
      - match: "billing"
        agent: billing_agent
      - match: "sales"
        agent: sales_agent
      - default: true              # Catch-all
        agent: general_support
```
Rules are evaluated in order. First match wins. If no rule matches and no default is set, the `default_agent` from the module config is used.

### Reply Mode

Controls what happens with the agent's response after activation.

| Mode | Behavior |
|------|----------|
| `"auto"` | Agent's final response is automatically sent back on the originating channel. No agent action needed. |
| `"none"` | Response is not sent anywhere. Agent must explicitly use `channels.send_message()` or `channels.reply()`. |
| `"explicit"` | Same as `"none"` — agent decides where to send. |

**`reply: auto` is the recommended mode for conversational channels** (WhatsApp, SMS, email, Telegram). The agent just responds naturally, and the system handles delivery.

---

## Adapters Reference

### Cron

Schedule-based trigger. Fires events at specified times. **Inbound only.**

```yaml
providers:
  daily_report:
    adapter: cron
    config:
      schedule: "0 9 * * 1-5"         # Standard 5-field cron expression
      message: "Generate the daily report"  # Optional message template
    activation:
      agent: reporter
```
| Config field | Type | Default | Description |
|-------------|------|---------|-------------|
| `schedule` | string | required | 5-field cron expression |
| `message` | string | `""` | Message template for the event |

**Cron expression format**: `minute hour day-of-month month day-of-week`

| Expression | Meaning |
|-----------|---------|
| `* * * * *` | Every minute |
| `0 * * * *` | Every hour |
| `0 9 * * 1-5` | 9 AM Monday-Friday |
| `0 0 1 * *` | Midnight on the 1st of each month |
| `*/5 * * * *` | Every 5 minutes |
| `0 9,18 * * *` | 9 AM and 6 PM daily |

Uses `croniter` for precise cron parsing if available, falls back to minute-step scan.

### File Watcher

Polls for new files matching glob patterns. **Inbound only.**

```yaml
providers:
  csv_inbox:
    adapter: file_watcher
    config:
      paths:                           # Glob patterns to watch
        - "./inbox/*.csv"
        - "./uploads/*.xlsx"
      poll_interval: 5.0               # Seconds between polls (default: 5)
      message: "New file: {{event.payload.filename}}"
    activation:
      agent: data_processor
```
| Config field | Type | Default | Description |
|-------------|------|---------|-------------|
| `paths` | list[string] | required | Glob patterns |
| `poll_interval` | float | `5.0` | Seconds between scans |
| `message` | string | `""` | Message template |

**Event payload** contains:
```json
{
  "path": "./inbox/report.csv",
  "resolved_path": "/home/user/app/inbox/report.csv",
  "filename": "report.csv",
  "size": 1024,
  "modified": 1711540800.0
}
```

Existing files are ignored on startup — only newly created files trigger events. Deduplication uses an in-memory seen set (capped at 10,000 entries).

### Webhook

Bidirectional HTTP. Receives POST requests (inbound) and/or sends POST requests (outbound).

```yaml
providers:
  # Inbound only
  github_hooks:
    adapter: webhook
    config:
      inbound_path: "/hook/github"
      auth: signature
      signature_secret: "{{secret.GITHUB_SECRET}}"
      signature_header: "X-Hub-Signature-256"
    activation:
      message: "GitHub: {{event.payload.action}} on {{event.payload.repository.name}}"

  # Outbound only (Slack notification)
  slack_alerts:
    adapter: webhook
    config:
      url: "{{secret.SLACK_WEBHOOK}}"
      headers:
        Content-Type: "application/json"

  # Bidirectional (receive + reply)
  whatsapp:
    adapter: webhook
    config:
      inbound_path: "/hook/whatsapp"
      auth: signature
      signature_secret: "{{secret.WA_SECRET}}"
      url: "{{secret.WA_API_URL}}"
      headers:
        Authorization: "Bearer {{secret.WA_TOKEN}}"
    activation:
      session: "wa-{{event.source}}"
      reply: auto
```
| Config field | Type | Default | Description |
|-------------|------|---------|-------------|
| **Inbound** | | | |
| `inbound_path` | string | `""` | URL path to listen on (empty = no inbound) |
| `auth` | string | `"none"` | Auth mode: `"none"`, `"signature"`, `"api_key"` |
| `signature_secret` | string | `""` | HMAC shared secret (for `auth: signature`) |
| `signature_header` | string | `"X-Signature-256"` | Header containing the signature |
| `api_key` | string | `""` | Expected API key (for `auth: api_key`) |
| `max_payload_bytes` | int | `1048576` | Max payload size (1 MB) |
| **Outbound** | | | |
| `url` | string | `""` | Target URL for outbound POST (empty = no outbound) |
| `headers` | dict | `{}` | Custom HTTP headers |
| `timeout` | float | `10.0` | Request timeout in seconds |

#### Webhook Authentication

**HMAC Signature** (`auth: signature`):
The adapter verifies inbound requests using HMAC-SHA256 with constant-time comparison (`hmac.compare_digest`). Supports both raw hex and prefixed signatures (`sha256=...`).

```yaml
config:
  auth: signature
  signature_secret: "{{secret.WEBHOOK_SECRET}}"
  signature_header: "X-Hub-Signature-256"    # GitHub format
```
**API Key** (`auth: api_key`):
Verifies the `X-API-Key` header using constant-time comparison.

```yaml
config:
  auth: api_key
  api_key: "{{secret.WEBHOOK_API_KEY}}"
```
**No auth** (`auth: none`):
No verification. Use only for testing or when the endpoint is behind a reverse proxy that handles auth.

#### Webhook Security Details

1. **Payload size check** — Enforced at raw byte level BEFORE JSON parsing. Prevents OOM attacks.
2. **Content-Type whitelist** — Only `application/json`, `application/x-www-form-urlencoded`, `text/plain` accepted.
3. **Payload sanitization** — Strips `__proto__`, `__class__`, `constructor` and all `__*`/`$$*` keys. Limits nesting depth (10), string length (10K chars), dict keys (200), list items (500).
4. **Header stripping** — `Authorization`, `Cookie`, `X-API-Key`, `X-Signature-*` removed from event metadata.
5. **SSRF protection** — Outbound URLs validated against private IP blocklist (RFC1918, loopback, link-local, multicast).

### Email

IMAP polling inbound + SMTP outbound. **Bidirectional.**

```yaml
providers:
  support_email:
    adapter: email
    config:
      imap:
        host: imap.gmail.com
        port: 993                      # Default: 993 (IMAP SSL)
        user: "{{env.SUPPORT_EMAIL}}"
        password: "{{secret.EMAIL_APP_PASSWORD}}"
        folder: INBOX                  # Default: INBOX
      smtp:
        host: smtp.gmail.com
        port: 587                      # Default: 587 (STARTTLS)
        user: "{{env.SUPPORT_EMAIL}}"
        password: "{{secret.EMAIL_APP_PASSWORD}}"
      poll_interval: 30                # Seconds between IMAP checks
      from_address: "support@company.com"  # Sender address for outbound
    activation:
      session: "email-{{event.source}}"
      reply: auto                      # Reply to the sender automatically
```
| Config field | Type | Default | Description |
|-------------|------|---------|-------------|
| `imap.host` | string | `""` | IMAP server (empty = no inbound) |
| `imap.port` | int | `993` | IMAP port |
| `imap.user` | string | `""` | IMAP username |
| `imap.password` | string | `""` | IMAP password |
| `imap.folder` | string | `"INBOX"` | IMAP folder to monitor |
| `smtp.host` | string | `""` | SMTP server (empty = no outbound) |
| `smtp.port` | int | `587` | SMTP port |
| `smtp.user` | string | `""` | SMTP username |
| `smtp.password` | string | `""` | SMTP password |
| `poll_interval` | float | `60.0` | Seconds between IMAP polls |
| `from_address` | string | smtp user | Sender email address |

**Inbound event payload**:
```json
{
  "uid": "123",
  "from": "client@example.com",
  "to": "support@company.com",
  "subject": "Help with my account",
  "body": "Hello, I need assistance with...",
  "date": "Thu, 27 Mar 2026 10:30:00 +0100",
  "message_id": "<abc123@mail.example.com>"
}
```

With `reply: auto`, the agent's response is sent as a reply email with proper `In-Reply-To` and `References` headers for threading.

**Gmail setup**: Use an [App Password](https://myaccount.google.com/apppasswords), not your account password. Enable IMAP in Gmail settings.

### Telegram

Telegram Bot API via long polling (inbound) and sendMessage REST (outbound). **Bidirectional.**

Requires: `pip install aiohttp`

```yaml
providers:
  telegram_bot:
    adapter: telegram
    config:
      token: "{{secret.TELEGRAM_BOT_TOKEN}}"
      poll_timeout: 30                   # Long poll timeout (seconds)
      # allowed_chat_ids: [123456789]    # Optional: restrict to specific chats
    activation:
      message: "{{event.payload.text}}"
      context: "Telegram user: {{event.payload.display_name}} (chat {{event.payload.chat_id}})"
      reply: auto
      session: "tg-{{event.payload.chat_id}}"
```
| Config field | Type | Default | Description |
|-------------|------|---------|-------------|
| `token` | string | required | Bot token from @BotFather |
| `poll_timeout` | int | `30` | Long polling timeout (seconds) |
| `allowed_chat_ids` | list[int] | `[]` | Restrict to these chat IDs (empty = all) |

**Setup**: Talk to [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, follow the prompts, and copy the token.

**Inbound event payload**:
```json
{
  "chat_id": 123456789,
  "message_id": 42,
  "from_id": 123456789,
  "username": "johndoe",
  "first_name": "John",
  "display_name": "John Doe",
  "chat_type": "private",
  "text": "Hello bot!"
}
```

Messages from bots (including itself) are automatically ignored. Supports Markdown formatting in replies with automatic fallback to plain text if Markdown parsing fails.

### Discord

Discord Bot API via WebSocket Gateway (inbound) and REST API (outbound). **Bidirectional.**

Requires: `pip install aiohttp`

```yaml
providers:
  discord_bot:
    adapter: discord
    config:
      token: "{{secret.DISCORD_BOT_TOKEN}}"
      # allowed_channel_ids: ["123456"]  # Optional: restrict to specific channels
      # allowed_guild_ids: ["789012"]    # Optional: restrict to specific servers
    activation:
      message: "{{event.payload.text}}"
      context: "Discord user: {{event.payload.display_name}} in channel {{event.payload.channel_id}}"
      reply: auto
      session: "discord-{{event.payload.channel_id}}"
```
| Config field | Type | Default | Description |
|-------------|------|---------|-------------|
| `token` | string | required | Bot token from Discord Developer Portal |
| `allowed_channel_ids` | list[string] | `[]` | Restrict to these channel IDs |
| `allowed_guild_ids` | list[string] | `[]` | Restrict to these server IDs |

**Setup**:
1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and create an application.
2. **Bot** tab: reset token and copy it. Enable **Message Content Intent**.
3. **OAuth2** tab: URL Generator with scope `bot`, permissions: Send Messages, Read Message History, View Channels. Open the URL to invite the bot to your server.

**Inbound event payload**:
```json
{
  "channel_id": "1234567890",
  "guild_id": "9876543210",
  "message_id": "111222333",
  "author_id": "444555666",
  "username": "johndoe",
  "display_name": "John Doe",
  "text": "Hello bot!"
}
```

Messages from bots (including the bot itself) are automatically ignored. Uses WebSocket Gateway with heartbeat for real-time message delivery (no polling delay).

### Slack

Slack Bot via Socket Mode WebSocket (inbound) and Web API (outbound). **Bidirectional.**

Requires: `pip install aiohttp`

```yaml
providers:
  slack_bot:
    adapter: slack
    config:
      bot_token: "{{secret.SLACK_BOT_TOKEN}}"   # xoxb-...
      app_token: "{{secret.SLACK_APP_TOKEN}}"   # xapp-...
      # allowed_channel_ids: ["C0123456789"]    # Optional: restrict to specific channels
    activation:
      message: "{{event.payload.text}}"
      context: "Slack user: {{event.payload.user_id}} in channel {{event.payload.channel_id}}"
      reply: auto
      session: "slack-{{event.payload.channel_id}}"
```
| Config field | Type | Default | Description |
|-------------|------|---------|-------------|
| `bot_token` | string | required | Bot User OAuth Token (`xoxb-...`) |
| `app_token` | string | required | App-Level Token (`xapp-...`) for Socket Mode |
| `allowed_channel_ids` | list[string] | `[]` | Restrict to these channel IDs |

**Setup**:
1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app (From scratch).
2. **Settings > Socket Mode**: enable it and generate an App-Level Token (`xapp-...`).
3. **Features > Event Subscriptions**: enable and subscribe to bot events: `message.channels`, `message.im`. Save changes.
4. **Features > OAuth & Permissions**: add scopes: `chat:write`, `channels:history`, `channels:read`, `im:history`, `im:read`.
5. **Install App** to your workspace and copy the Bot Token (`xoxb-...`).
6. Invite the bot to a channel: in Slack, go to the channel settings > Integrations > Add apps > select your app.

**Inbound event payload**:
```json
{
  "channel_id": "C0123456789",
  "user_id": "U0350BYF78D",
  "text": "Hello bot!",
  "ts": "1711839600.000100",
  "channel_type": "channel",
  "thread_ts": ""
}
```

With `reply: auto`, the agent's response is posted as a **thread reply** to the original message. Messages from bots (including itself) and message subtypes (join, leave, etc.) are automatically ignored. Socket Mode uses WSS (no public URL needed).

### Voice

Bidirectional voice calls via pluggable backends, TTS, and STT providers. The voice adapter handles text only — audio transport is delegated to interchangeable components. **Bidirectional.**

Requires: `pip install aiohttp` (+ `pip install edge-tts` for Edge TTS)

```yaml
providers:
  phone:
    adapter: voice
    config:
      backend: websocket           # or: twilio_cr, livekit, pipecat
      language: fr
      welcome: "Bonjour, comment puis-je vous aider ?"
      backend_config:
        port: 8766
        tts:
          provider: edge           # or: elevenlabs, openai, http, browser
          voice: fr-FR-DeniseNeural
        stt:
          provider: browser        # or: deepgram, openai, http
    activation:
      message: "{{event.payload.transcript}}"
      context: "Voice call from {{event.payload.caller}} ({{event.payload.direction}})"
      reply: auto
      session: "call-{{event.payload.call_id}}"
```
| Config field | Type | Default | Description |
|-------------|------|---------|-------------|
| `backend` | string | required | Voice backend: `twilio_cr`, `websocket` |
| `language` | string | `"en"` | Language code for STT/TTS |
| `welcome` | string | `""` | Greeting spoken when call connects |
| `backend_config` | dict | `{}` | Backend-specific configuration (see below) |

**Available backends:**

| Backend | Transport | Phone | Browser | Self-hosted |
|---------|-----------|-------|---------|-------------|
| `twilio_cr` | Twilio ConversationRelay | Yes | No | No |
| `websocket` | Generic WebSocket | No | Yes | Yes |

**TTS providers** (text-to-speech):

| Provider | Quality | Latency | Cost | Self-hosted |
|----------|---------|---------|------|-------------|
| `edge` | High (neural) | ~100ms | Free | No |
| `elevenlabs` | Excellent | ~75ms | $0.10/min | No |
| `openai` | Very good | ~200ms | $0.015/1K chars | No |
| `http` | Depends on model | Varies | Free (self-hosted) | Yes |
| `browser` | Basic (robotic) | Instant | Free | N/A |

```yaml
# Edge TTS (free, 12+ languages, neural voices)
tts:
  provider: edge
  voice: fr-FR-DeniseNeural     # or: en-US-AriaNeural, es-ES-ElviraNeural, ...
  rate: "+5%"                    # Speed adjustment

# ElevenLabs (premium quality)
tts:
  provider: elevenlabs
  api_key: "{{secret.ELEVENLABS_KEY}}"
  voice_id: "21m00Tcm4TlvDq8ikWAM"
  model: eleven_flash_v2_5

# OpenAI TTS (or any compatible endpoint)
tts:
  provider: openai
  api_key: "{{secret.OPENAI_KEY}}"
  base_url: "https://api.openai.com/v1"  # or your local endpoint
  voice: nova                    # alloy, echo, fable, onyx, nova, shimmer
  model: tts-1

# Local model (Coqui XTTS, Piper, MaryTTS, etc.)
tts:
  provider: http
  url: "http://localhost:5002/api/tts"
  voice: my_custom_voice
```
**STT providers** (speech-to-text):

| Provider | Quality | Latency | Cost | Self-hosted |
|----------|---------|---------|------|-------------|
| `deepgram` | Excellent | ~150ms | $0.0043/min | No |
| `openai` | Very good | ~500ms | $0.006/min | No |
| `http` | Depends on model | Varies | Free (self-hosted) | Yes |
| `browser` | Good | Instant | Free | N/A (Web Speech API) |

```yaml
# Deepgram Nova (fastest, most accurate)
stt:
  provider: deepgram
  api_key: "{{secret.DEEPGRAM_KEY}}"
  model: nova-3

# OpenAI Whisper (or local faster-whisper via compatible API)
stt:
  provider: openai
  api_key: "{{secret.OPENAI_KEY}}"
  base_url: "http://localhost:8080/v1"  # local faster-whisper server

# Local model (faster-whisper, Vosk, Kaldi, etc.)
stt:
  provider: http
  url: "http://localhost:9000/asr"
```
**Twilio ConversationRelay backend** (`twilio_cr`):

Twilio handles STT/TTS internally — the `tts` and `stt` config sections are ignored.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `8765` | Local server port for TwiML + WebSocket |
| `public_url` | string | `""` | Public URL (ngrok) for Twilio webhook |
| `tts_provider` | string | `"google"` | Twilio TTS: `google`, `amazon`, `elevenlabs` |
| `stt_provider` | string | `"deepgram"` | Twilio STT: `deepgram`, `google` |
| `voice` | string | `""` | Voice name (Twilio provider-specific) |
| `interruptible` | string | `"speech"` | Barge-in: `none`, `dtmf`, `speech`, `any` |

Setup: Create a Twilio account, buy a phone number, run ngrok, configure the number's Voice webhook to `https://your-ngrok.io/voice/incoming`.

**WebSocket backend** (`websocket`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `8766` | WebSocket server port |
| `tts` | dict | `{}` | TTS provider config (see above) |
| `stt` | dict | `{}` | STT provider config (see above) |

JSON + binary protocol: client sends `{"type": "transcript", "text": "..."}`, server responds with audio chunks (binary) or text fallback. Works with browser Web Speech API, or any custom client.

**Inbound event payload** (all backends):

```json
{
  "call_id": "call-6f91d2ab24d5",
  "transcript": "Bonjour, comment ca va ?",
  "caller": "+33612345678",
  "direction": "inbound",
  "language": "fr"
}
```

The voice adapter is fully composable: swap backend, TTS, and STT independently via YAML. The `http` providers enable self-hosted open-source models — point at any local server running Coqui XTTS, Piper, faster-whisper, Vosk, etc. The agent code, activation pipeline, and session management work identically regardless of the voice stack.

### RSS

Polls RSS/Atom feeds for new entries. **Inbound only.**

Requires: `pip install feedparser`

```yaml
providers:
  tech_news:
    adapter: rss
    config:
      feed_url: "https://hnrss.org/newest"
      poll_interval: 600               # 10 minutes
      max_entries_per_poll: 10
    activation:
      filter:
        - field: event.payload.title
          contains: "AI"
      agent: researcher
      message: "New article: {{event.payload.title}}\n{{event.payload.link}}"
```
| Config field | Type | Default | Description |
|-------------|------|---------|-------------|
| `feed_url` | string | required | RSS/Atom feed URL |
| `poll_interval` | float | `300.0` | Seconds between polls |
| `max_entries_per_poll` | int | `10` | Max entries processed per poll |

**Event payload**:
```json
{
  "title": "New AI Framework Announced",
  "link": "https://example.com/article",
  "summary": "A new framework for...",
  "published": "2026-03-27T10:00:00Z",
  "author": "John Doe",
  "entry_id": "https://example.com/article"
}
```

Existing entries are skipped on first poll (only new entries trigger events). Deduplication by entry ID/link.

### Log

Writes messages to Python logging. **Outbound only.** Useful for debugging, audit trails, and development.

```yaml
providers:
  debug:
    adapter: log
    config:
      level: info                      # debug, info, warning, error
      logger: "digitorn.channels.output"  # Logger name
```
### Queue

Bridges to the QueueModule for event-driven inter-app messaging. **Bidirectional.**

```yaml
providers:
  order_events:
    adapter: queue
    config:
      queue: orders                    # Queue name
      topics: ["order.created", "order.updated"]  # Topic filter
      poll_interval: 5.0
    activation:
      session: "order-{{event.payload.order_id}}"
      message: "New order event: {{event.payload.type}}"
```
The queue adapter uses the ServiceBus to call the `queue` module's `receive` and `publish` actions. Requires the `queue` module to be loaded.

---

## Agent Actions

The channels module exposes 12 actions that agents can call at runtime.

### Sending Messages

| Action | Risk | Description |
|--------|------|-------------|
| `channels.send_message` | medium | Send a message through a specific provider |
| `channels.reply` | medium | Reply on the originating channel (only during activation) |
| `channels.broadcast` | high | Send the same message to multiple providers |

**send_message** — Send to any provider:
```
channels.send_message(
  provider: "slack_alerts",
  text: "Server CPU at 95%!",
  subject: "Alert",
  recipient: "#ops-channel"    # Optional: override recipient
)
```

**reply** — Reply on the channel that triggered this activation:
```
channels.reply(text: "Thanks for contacting support. I'm looking into your issue.")
```

Only available during a channel-triggered activation. Uses the `reply_context` from the inbound event for correct threading (email In-Reply-To, Slack thread_ts, etc.).

**broadcast** — Send to multiple channels at once:
```
channels.broadcast(
  providers: ["slack_alerts", "email_team", "debug"],
  text: "Critical: database connection lost",
  subject: "Database Alert"
)
```

### Provider Management

| Action | Risk | Description |
|--------|------|-------------|
| `channels.list_providers` | low | List all providers and their status |
| `channels.provider_status` | low | Detailed status of a specific provider |
| `channels.pause_provider` | medium | Pause a provider's inbound listener |
| `channels.resume_provider` | medium | Resume a paused listener |

### Observability

| Action | Risk | Description |
|--------|------|-------------|
| `channels.provider_history` | low | Recent inbound/outbound event history |
| `channels.stats` | low | Aggregate statistics (events received/sent, sessions, etc.) |

### Testing

| Action | Risk | Description |
|--------|------|-------------|
| `channels.simulate_event` | medium | Simulate an inbound event for testing |
| `channels.test_send` | medium | Send a test message to verify outbound connectivity |

---

## Security Model

Security is the cornerstone of Digitorn. The channels module enforces 16 security measures across all layers.

### Inbound Protection

| # | Measure | Details |
|---|---------|---------|
| 1 | **Payload size limit** | Checked at raw byte level BEFORE JSON parsing. Default 1 MB. Prevents OOM attacks from oversized payloads. |
| 2 | **HMAC signature verification** | SHA-256 with constant-time comparison via `hmac.compare_digest()`. Supports raw hex and prefixed formats (`sha256=...`). |
| 3 | **API key authentication** | Constant-time comparison. Key stored encrypted in SecretStore. |
| 4 | **Content-Type whitelist** | Only `application/json`, `application/x-www-form-urlencoded`, `text/plain` accepted. Everything else rejected with 415. |
| 5 | **Payload sanitization** | Strips prototype pollution keys (`__proto__`, `__class__`, `constructor`, `__import__`, all `__*` and `$$*` prefixes). Limits: nesting depth 10, string length 10K chars, 200 dict keys, 500 list items per level. Binary data converted to empty string. |
| 6 | **Sensitive header stripping** | `Authorization`, `Cookie`, `X-API-Key`, `X-Signature-*` removed from event metadata before reaching the agent. |

### Outbound Protection

| # | Measure | Details |
|---|---------|---------|
| 7 | **SSRF protection** | All outbound URLs validated against private IP blocklist: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `169.254.0.0/16`, `::1/128`, `fc00::/7`, `fe80::/10`. DNS resolved once and pinned. |
| 8 | **Secret filtering** | Outbound messages scanned for API key patterns (OpenAI `sk-*`, Anthropic `sk-ant-*`, GitHub `ghp_*`, AWS `AKIA*`, JWT `eyJ*`, Bearer tokens, Digitorn `dk_*`, Basic auth). Matches replaced with `[REDACTED]`. |
| 9 | **Header masking in logs** | Sensitive headers (`Authorization`, `Cookie`, `X-API-Key`) masked as `***masked***` in all log output. |

### Template Safety

| # | Measure | Details |
|---|---------|---------|
| 10 | **No eval/exec** | Pure `str.replace()` substitution only. No Jinja2, no code execution, no expression evaluation. |
| 11 | **No runtime secret access** | `{{secret.*}}` and `{{env.*}}` are blocked at runtime. Secrets are resolved at compile time by the YAML compiler — never at template render time. Attempts are logged as warnings. |
| 12 | **Single-pass substitution** | No recursive expansion. `{{{{nested}}}}` produces `{{nested}}`, not a resolved value. Prevents template injection attacks where user-controlled data contains `{{...}}`. |
| 13 | **Max output length** | 256 KB limit on rendered templates. Prevents expansion bombs from large variable values. |

### Isolation

| # | Measure | Details |
|---|---------|---------|
| 14 | **Config isolation** | Each adapter receives a shallow copy of its config dict. Adapters cannot access other adapters' configuration. |
| 15 | **Credential isolation** | Secrets are resolved at compile time and passed as config values. Adapters have no access to the SecretStore. |
| 16 | **Concurrency control** | Per-provider semaphore (default: 5) limits concurrent activations. Prevents resource exhaustion from event floods. Shared sessions use `asyncio.Lock` per session key for serialization. |

### Capabilities Integration

The channels module integrates with Digitorn's capability system:

```yaml
capabilities:
  grant:
    - channels.list_providers          # Agent can list channels
    - channels.provider_history        # Agent can see event history
    - channels.stats                   # Agent can see statistics
    - channels.reply                   # Agent can reply on originating channel
  approve:
    - channels.send_message            # Requires human approval
    - channels.broadcast               # Requires human approval
  deny:
    - channels.pause_provider          # Agent cannot pause channels
    - channels.simulate_event          # Agent cannot simulate events
```
---

## Creating Custom Adapters

You can create custom adapters for any transport (Telegram, Discord, Twilio SMS, MQTT, Kafka, etc.) by implementing the `BaseChannelAdapter` protocol.

### Adapter Protocol

```python
from digitorn.modules.channels.adapter import (
    BaseChannelAdapter,
    AdapterCapabilities,
    InboundCallback,
    InboundEvent,
    make_event_id,
)
from digitorn.core.app.channels.base import ChannelPayload, DeliveryResult


class MyAdapter(BaseChannelAdapter):
    # ── Required class attributes ──
    CHANNEL_ID = "my_transport"           # Unique ID
    CHANNEL_NAME = "My Transport"         # Human-readable name
    CHANNEL_VERSION = "1.0.0"
    SUPPORTS_INBOUND = True               # Can receive events?
    SUPPORTS_OUTBOUND = True              # Can send messages?

    def __init__(self, channel_config=None):
        super().__init__(channel_config=channel_config)
        # Parse your config here
        self._api_key = (channel_config or {}).get("api_key", "")

    # ── Inbound: listen for events ──
    async def start_listener(self, callback: InboundCallback) -> None:
        """Start listening. Call callback(event) for each inbound event."""
        # Your listening loop here (polling, websocket, etc.)
        while True:
            data = await self._poll_for_messages()
            for msg in data:
                event = InboundEvent(
                    event_id=make_event_id(),
                    provider_id="",            # Set by the module
                    adapter_type="my_transport",
                    source=msg["sender"],
                    message=msg["text"],
                    payload=msg,
                    reply_context={            # Used by reply:auto
                        "_app_id": "",
                        "chat_id": msg["chat_id"],
                    },
                )
                await callback(event)

    async def stop_listener(self) -> None:
        """Stop listening and cleanup."""
        pass

    # ── Outbound: send messages ──
    async def deliver(self, app_id, payload, config) -> DeliveryResult:
        """Send a message through this transport."""
        try:
            response = await self._send_message(
                chat_id=config.get("chat_id", self._default_chat),
                text=payload.message,
            )
            return DeliveryResult(
                success=True,
                channel_id=self.CHANNEL_ID,
                delivery_id=response["message_id"],
            )
        except Exception as exc:
            return DeliveryResult(
                success=False,
                channel_id=self.CHANNEL_ID,
                error=str(exc)[:200],
                retryable=True,
            )

    # ── Reply (used by reply:auto) ──
    async def send_reply(self, reply_context, text, payload=None):
        """Reply using context from inbound event."""
        effective = payload or ChannelPayload(message=text)
        return await self.deliver(
            app_id=reply_context.get("_app_id", ""),
            payload=effective,
            config=reply_context,
        )

    # ── Capabilities ──
    def adapter_capabilities(self):
        return AdapterCapabilities(
            supports_inbound=True,
            supports_outbound=True,
            supports_rich_text=True,
            supports_threading=True,
        )
```

### Registering Custom Adapters

Register your adapter in the adapter registry:

```python
from digitorn.modules.channels.adapters import register_adapter
from my_package.telegram_adapter import TelegramAdapter

register_adapter("telegram", TelegramAdapter)
```

Then use it in YAML:

```yaml
providers:
  telegram_bot:
    adapter: telegram
    config:
      bot_token: "{{secret.TELEGRAM_TOKEN}}"
    activation:
      session: "tg-{{event.source}}"
      reply: auto
```
### Adapter Security Checklist

When building a custom adapter, follow these security practices:

1. **Never store secrets in the adapter instance** — receive them via `channel_config` (resolved at compile time)
2. **Override `validate_inbound()`** — verify signatures, API keys, or other auth on inbound requests
3. **Override `max_inbound_payload_bytes()`** — set a size limit appropriate for your transport
4. **Build `reply_context` from verified data only** — never include raw user input in the reply context
5. **Use `sanitize_payload()` from `channels.security`** — sanitize all inbound data before creating the `InboundEvent`
6. **Handle timeouts** — set finite timeouts on all network operations
7. **Return `DeliveryResult` with `retryable`** — distinguish transient (retry) from permanent (don't retry) errors

---

## Complete Application Examples

### IT Support Bot (Multi-channel)

An agent that receives requests via WhatsApp, email, and GLPI webhooks, looks up the client, routes to the right specialist, and replies automatically.

```yaml
app:
  app_id: it-support
  name: IT Support Bot

variables:
  workspace: ./workspace

modules:
  database:
    setup:
      - action: connect
        params:
          connection_id: main
          driver: sqlite
          database: "./data/support.db"
  memory: {}
  rag:
    setup:
      - action: create_knowledge_base
        params: { name: it_procedures, path: ./docs/procedures/ }
  channels:
    config:
      default_agent: receptionist
      max_turns: 30
      timeout: 120
      providers:
        # ── WhatsApp (via Twilio webhook) ──
        whatsapp:
          adapter: webhook
          config:
            inbound_path: /hook/whatsapp
            auth: signature
            signature_secret: "{{secret.TWILIO_WA_SECRET}}"
            url: "https://api.twilio.com/2010-04-01/Accounts/{{env.TWILIO_SID}}/Messages.json"
            headers:
              Authorization: "Basic {{secret.TWILIO_AUTH_BASE64}}"
          activation:
            prepare:
              - action: database.fetch_results
                params:
                  query: "SELECT * FROM clients WHERE phone = '{{event.source}}'"
                as: caller
              - action: database.fetch_results
                params:
                  query: "SELECT id,title,status FROM tickets WHERE client_id={{caller.id}} ORDER BY created_at DESC LIMIT 5"
                as: recent_tickets
            route:
              field: caller.plan
              rules:
                - match: premium
                  agent: vip_support
                - default: true
                  agent: receptionist
            session: "wa-{{event.source}}"
            context: |
              Canal: WhatsApp
              Client: {{caller.name}} (plan {{caller.plan}}, depuis {{caller.created_at}})
              Tickets recents: {{recent_tickets}}
            reply: auto

        # ── Email support ──
        email:
          adapter: email
          config:
            imap:
              host: imap.gmail.com
              user: "{{env.SUPPORT_EMAIL}}"
              password: "{{secret.EMAIL_APP_PASSWORD}}"
            smtp:
              host: smtp.gmail.com
              port: 587
              user: "{{env.SUPPORT_EMAIL}}"
              password: "{{secret.EMAIL_APP_PASSWORD}}"
            poll_interval: 30
            from_address: "support@company.com"
          activation:
            filter:
              - field: event.payload.subject
                not_equals: ""
            prepare:
              - action: database.fetch_results
                params:
                  query: "SELECT * FROM clients WHERE email = '{{event.source}}'"
                as: sender
            session: "email-{{event.source}}"
            context: "Canal: Email\nContact: {{sender.name}} ({{sender.plan}})"
            reply: auto

        # ── GLPI webhooks ──
        glpi:
          adapter: webhook
          config:
            inbound_path: /hook/glpi
            auth: api_key
            api_key: "{{secret.GLPI_WEBHOOK_KEY}}"
          activation:
            filter:
              - field: event.payload.status
                equals: "new"
            prepare:
              - action: database.fetch_results
                params:
                  query: "SELECT * FROM clients WHERE glpi_id = {{event.payload.users_id}}"
                as: requester
              - action: rag.query
                params:
                  knowledge_base: it_procedures
                  query: "{{event.payload.itilcategories_name}}"
                as: procedure
            route:
              field: event.payload.itilcategories_name
              rules:
                - match: "Reseau"
                  agent: network_expert
                - match: "Logiciel"
                  agent: software_expert
                - default: true
                  agent: receptionist
            session: "ticket-{{event.payload.id}}"
            context: |
              Ticket GLPI #{{event.payload.id}}
              Client: {{requester.name}} ({{requester.plan}})
              Categorie: {{event.payload.itilcategories_name}}
              Procedure suggeree: {{procedure}}
            message: "{{event.payload.name}}\n\n{{event.payload.content}}"

        # ── Slack alerts (outbound only) ──
        slack:
          adapter: webhook
          config:
            url: "{{secret.SLACK_WEBHOOK}}"

        # ── Daily report ──
        daily_report:
          adapter: cron
          config:
            schedule: "0 8 * * 1-5"
          activation:
            agent: reporter
            message: "Generate the daily IT support report for yesterday."

agents:
  - id: receptionist
    brain: { provider: anthropic, model: claude-sonnet-4-20250514 }
    role: |
      Tu es la receptionniste du support IT. Tu accueilles les demandes,
      identifies le besoin, et resous les problemes simples. Pour les cas
      complexes, tu transfères au bon specialiste.
      Tu peux envoyer des alertes sur Slack pour les problemes critiques.

  - id: vip_support
    brain: { provider: anthropic, model: claude-opus-4-20250514 }
    role: |
      Support VIP premium. Service proactif, acces a toutes les ressources.
      Temps de reponse prioritaire. Consulte toujours l'historique client.

  - id: network_expert
    brain: { provider: anthropic, model: claude-sonnet-4-20250514 }
    role: "Expert reseau. Diagnostique VPN, DNS, firewall, connectivite."

  - id: software_expert
    brain: { provider: anthropic, model: claude-sonnet-4-20250514 }
    role: "Expert logiciel. Installation, configuration, depannage applicatif."

  - id: reporter
    brain: { provider: anthropic, model: claude-sonnet-4-20250514 }
    role: |
      Genere des rapports quotidiens depuis les donnees. Envoie sur Slack.

execution:
  mode: background

capabilities:
  default_policy: auto
  grant:
    - module: channels
      actions: [list_providers, reply, send_message, stats]
  approve:
    - module: channels
      actions: [broadcast]
```
### Data Processing Pipeline

An agent that watches a directory for CSV files, processes them, and sends reports.

```yaml
app:
  app_id: data-pipeline
  name: CSV Processing Pipeline

modules:
  filesystem: {}
  database:
    setup:
      - action: connect
        params:
          connection_id: main
          driver: sqlite
          database: "./data/pipeline.db"
  shell: {}   # Use `Bash` for CSV/Excel/PDF manipulation (e.g. `csvkit`, `libreoffice --convert-to`)
  channels:
    config:
      providers:
        inbox:
          adapter: file_watcher
          config:
            paths: ["./inbox/*.csv"]
            poll_interval: 10
          activation:
            message: "New CSV file to process: {{event.payload.filename}} ({{event.payload.size}} bytes)"

        reports_email:
          adapter: email
          config:
            smtp:
              host: smtp.gmail.com
              port: 587
              user: "{{env.REPORT_EMAIL}}"
              password: "{{secret.EMAIL_PWD}}"
            from_address: "reports@company.com"

        daily_digest:
          adapter: cron
          config:
            schedule: "0 18 * * 1-5"
          activation:
            message: "Generate the end-of-day processing digest."

agents:
  - id: main
    brain: { provider: openai, model: gpt-4o }
    role: |
      Data processing agent. When a CSV arrives:
      1. Read and analyze the file
      2. Import data into the database
      3. Generate a summary report (PDF)
      4. Email the report to the team
      5. Archive the processed file

execution:
  mode: background
```
### Monitoring Agent

An agent that monitors RSS feeds, APIs, and cron-based checks, and alerts on Slack.

```yaml
app:
  app_id: monitor
  name: Infrastructure Monitor

modules:
  http: {}
  memory: {}
  channels:
    config:
      providers:
        health_check:
          adapter: cron
          config:
            schedule: "*/5 * * * *"
          activation:
            message: "Run health checks on all monitored services."

        tech_feed:
          adapter: rss
          config:
            feed_url: "https://status.cloud.google.com/feed.atom"
            poll_interval: 300
          activation:
            filter:
              - field: event.payload.title
                contains: "incident"
            message: "Cloud incident: {{event.payload.title}}\n{{event.payload.link}}"

        slack:
          adapter: webhook
          config:
            url: "{{secret.SLACK_OPS_WEBHOOK}}"

        pagerduty:
          adapter: webhook
          config:
            url: "https://events.pagerduty.com/v2/enqueue"
            headers:
              Content-Type: "application/json"

agents:
  - id: main
    brain: { provider: anthropic, model: claude-sonnet-4-20250514 }
    role: |
      Infrastructure monitoring agent. Check service health every 5 minutes.
      Alert on Slack for warnings. Escalate to PagerDuty for critical issues.
      Track incident history in memory.

execution:
  mode: background

capabilities:
  default_policy: auto
  grant:
    - module: channels
      actions: [send_message, reply, stats]
```
---

## Integration with Other Modules

The channels module works seamlessly with all other Digitorn modules:

| Module | Integration |
|--------|------------|
| **database** | Prepare steps query the DB to enrich events. Agent uses DB during activation. |
| **rag** | Prepare steps search knowledge bases. Agent uses RAG to find answers. |
| **memory** | Agent remembers across shared sessions. Persistent context across events. |
| **filesystem** | Agent reads/writes files during activation. File watcher triggers on changes. |
| **spreadsheet** | Agent creates Excel/CSV reports, sends via email channel. |
| **pdf** | Agent generates PDF reports, sends via email channel. |
| **agent_spawn** | Agent spawns sub-agents during activation for parallel processing. |
| **http** | Agent makes API calls during activation. Webhook adapter uses HTTP. |
| **queue** | Queue adapter bridges to QueueModule for inter-app messaging. |
| **shell** | Agent runs commands during activation (with sandbox protection). |
| **mcp** | Agent uses MCP server tools (filesystem, GitHub, Gmail, etc.) during activation. |

The channels module is **only the I/O layer** — it handles how events arrive and how replies are sent. The agent inside an activation is a full Digitorn agent with access to all declared modules. Adding modules to the `modules:` block gives the agent more capabilities:

```yaml
# Discord bot with filesystem, database, and MCP tools
modules:
  filesystem:
    constraints:
      paths: ["/data/workspace"]
  database:
    setup:
      - action: connect
        params:
          connection_id: main
          driver: sqlite
          url: "sqlite:///data/workspace/app.db"
  mcp:
    config:
      servers:
        filesystem:
          command: npx
          args: ["-y", "@modelcontextprotocol/server-filesystem", "/data/reports"]
          sandbox:
            permissions: ["*"]
  channels:
    config:
      providers:
        discord_bot:
          adapter: discord
          config:
            token: "{{secret.DISCORD_BOT_TOKEN}}"
          activation:
            message: "{{event.payload.text}}"
            reply: auto
            session: "discord-{{event.payload.channel_id}}"

agents:
  - id: main
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        base_url: "https://api.deepseek.com/v1"
        api_key: "{{env.DEEPSEEK_API_KEY}}"
    role: |
      You are a Discord assistant with full access to the filesystem,
      a SQLite database, and MCP tools. Help users with their requests.

execution:
  mode: background
```
In this example, a Discord message triggers the agent which can read/write files, query a database, and use MCP tools — then reply on Discord. The same pattern works with any adapter (email, Telegram, cron, webhook, etc.).

---

## Backward Compatibility

### Legacy Triggers

The old `execution.triggers` system continues to work:

```yaml
# This still works — no changes needed
execution:
  mode: background
  triggers:
    - id: check
      type: cron
      schedule: "0 * * * *"
      message: "Hourly check"
```
When the `channels` module is **not loaded**, `background.py` uses the legacy trigger loops. When the `channels` module **is loaded**, it takes over all trigger handling with its richer pipeline.

### Legacy Output Channels

The old top-level `channels:` block for output-only channels still works:

```yaml
# This still works
channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{secret.SLACK_WEBHOOK}}"
```
For new apps, use the unified `modules: channels: config: providers:` syntax instead.

---

## Module Lifecycle

The channels module has a three-phase lifecycle that separates deployment from execution:

### Phase 1: Deploy (`on_config_update`)

Called when `digitorn app deploy` compiles the YAML. At this point:

- YAML config is parsed and validated
- Adapter instances are created and initialized (`on_start()`)
- Webhook tokens are generated
- **No listeners are started** — the module is in `ready` state

This phase ensures the config is valid before any network activity.

### Phase 2: Run (`start_listeners`)

Called by `run_background()` after the `RuntimeApp` has wired `_runtime_app` and `_hook_runner`. At this point:

- Inbound listener tasks are launched (cron loops, file watcher polls, webhook callbacks)
- All providers transition to `active` state
- `agent_turn()` is now available for activations

For daemon-deployed apps, this happens when the daemon starts or the app is first accessed. For `digitorn run`, this happens immediately.

### Phase 3: Shutdown (`on_stop`)

Called when the app is undeployed or the process exits:

- All listener tasks are cancelled
- Active activation tasks are cancelled
- Adapter `stop_listener()` and `on_stop()` are called
- Sessions are cleared

### Lifecycle diagram

```text
  on_config_update()          start_listeners()           on_stop()
  ─────────────────           ────────────────            ─────────
  Parse YAML config           Launch cron loops           Cancel listeners
  Create adapters             Launch file watchers        Cancel activations
  Validate config             Set webhook callbacks       Stop adapters
  Status: ready               Status: active              Cleanup sessions
```

---

## Error Handling

### Activation failures

When an activation fails (agent_turn error, timeout, etc.):

- The error is logged: `channel_activation_error provider=X event=Y error=Z`
- The provider's `last_error` field is updated (visible via `channels.provider_status()`)
- The activation task is cleaned up from `activation_tasks`
- Other activations continue normally — one failure doesn't block the provider

### Prepare step failures

If a `prepare:` step fails (e.g., database query returns an error):

- The pipeline logs the error and **skips the activation** for this event
- The event is still recorded in history
- No partial state leaks to subsequent steps

### Reply delivery failures

If `reply: auto` fails to send the response:

- The error is logged but **not** propagated to the agent
- The agent's work is still completed (its response is saved in session history)
- Use `channels.provider_history()` to see delivery failures

### Provider errors

If an adapter's listener crashes (e.g., IMAP connection lost):

- The listener task catches the exception and logs it
- The provider transitions to `error` state
- Use `channels.resume_provider()` to restart the listener

### No automatic retries

The channels module does **not** retry failed activations or deliveries automatically. This is by design — retries should be explicit (via the agent's logic or external orchestration) to avoid cascading failures.

---

## Migration Guide

### From legacy `execution.triggers`

**Before** (legacy triggers):

```yaml
execution:
  mode: background
  triggers:
    - id: hourly
      type: cron
      schedule: "0 * * * *"
      message: "Run hourly check"
    - id: inbox
      type: watch
      paths: ["/data/inbox/*.csv"]
      message: "New file: {{event.path}}"
```
**After** (channels module):

```yaml
modules:
  channels:
    config:
      providers:
        hourly:
          adapter: cron
          config:
            schedule: "0 * * * *"
          activation:
            message: "Run hourly check"
        inbox:
          adapter: file_watcher
          config:
            paths: ["/data/inbox/*.csv"]
          activation:
            message: "New file: {{event.payload.filename}} ({{event.payload.size}} bytes)"

execution:
  mode: background
```
Key differences:

- Triggers become `providers` with richer config
- File watcher gives structured payload (`event.payload.filename`, `event.payload.size`) instead of just `event.path`
- You gain filtering, prepare steps, routing, session management, and reply:auto
- You can mix inbound and outbound in the same config

### From legacy `channels:` block

**Before** (output-only channels):

```yaml
channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{secret.SLACK_WEBHOOK}}"
```
**After** (channels module):

```yaml
modules:
  channels:
    config:
      providers:
        slack_alerts:
          adapter: webhook
          config:
            url: "{{secret.SLACK_WEBHOOK}}"
```
Both systems can coexist in the same app — legacy channels continue to work alongside the module.

---

## Troubleshooting

### Provider not starting

Check the module logs:

```bash
digitorn run my-app.yaml --log-level debug 2>&1 | grep "channel_"
```

Common causes:

- Invalid adapter type (typo in `adapter:` field)
- Missing required config (e.g., `schedule` for cron, `paths` for file_watcher)
- Credentials not set (check `{{secret.*}}` variables are deployed)

### Events not triggering

1. Check the provider is active: agent calls `channels.list_providers()`
2. Check filters aren't too restrictive: temporarily remove `filter:` block
3. Check event history: agent calls `channels.provider_history(provider="my_provider")`
4. Use `channels.simulate_event()` to test the pipeline without a real event

### Replies not sending

1. Verify `reply: auto` is set in the activation config
2. Check the adapter supports outbound (`SUPPORTS_OUTBOUND = True`)
3. Verify outbound URL/credentials are configured
4. Check secret filtering isn't over-redacting: the agent's response might match a secret pattern

### Template not resolving

- Check dot-path syntax: `{{event.payload.field}}` not `{{event.payload[field]}}`
- Verify the field exists in the event data: use `expose_data: true` and check the workbench
- Remember: `{{secret.*}}` and `{{env.*}}` are blocked at runtime

### Debugging with simulate_event

Test your pipeline without real events:

```yaml
# Agent can call this action to inject a test event
channels.simulate_event(
  provider="my_webhook",
  payload={"action": "created", "id": 42},
  source="test",
  message="Test event"
)
```
This bypasses the adapter's inbound validation and injects directly into the pipeline — useful for testing filters, prepare steps, and routing.
