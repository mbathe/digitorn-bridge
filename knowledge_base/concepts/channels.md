---
id: channels
title: "Channels (bidirectional input/output adapters)"
type: concept
keywords: [channels, webhook, telegram, slack, discord, email, log, file_watcher, rss, queue, adapter, activation, reply, user_resolver, output, input, send_message, broadcast, provider]
related: [triggers, session-modes, execution-modes, capabilities]
source: packages/digitorn/modules/channels/module.py
---

# Channels - bidirectional input/output adapters

## What it is

The channels module provides **unified bidirectional communication** for Digitorn apps. A single `channels:` block in the YAML configures both:

- **Input adapters** (inbound) -- receive events from external sources (Telegram messages, Slack commands, webhook POSTs, email, RSS feeds, file changes, queue messages) and activate the agent.
- **Output channels** (outbound) -- the agent or scheduled jobs send messages to external systems (webhook POST, Slack channel, email, SMS, Telegram reply).

Each named entry under `channels:` is a **provider instance** with a type, config, and optional activation pipeline for inbound events.

## Two separate YAML locations

### 1. `channels:` top-level block (output only)

Used by the scheduler and watchers to route notifications. These are **output-only** channel instances referenced by name in `execution.default_channel` or per-job `output_channel`.

```yaml
channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{secret.SLACK_WEBHOOK}}"

  email_reports:
    type: gmail
    config:
      to_address: "{{secret.REPORT_EMAIL}}"
      from_name: "Digitorn Agent"

  sms_user:
    type: sms
    config:
      account_sid: "{{secret.TWILIO_SID}}"
      from_number: "+33600000000"
    user_resolver:
      module: database
      action: fetch_results
      params:
        query: "SELECT phone FROM users WHERE session_id = :session_id"
      mapping:
        to_number: phone
      cache_ttl: 300
```

### 2. `modules.channels:` block (bidirectional)

Used for full input+output channel providers with activation pipelines. This is where you configure Telegram bots, Slack bots, email listeners, etc.

```yaml
modules:
  channels:
    config:
      default_agent: main
      max_turns: 30
      timeout: 120
      providers:
        telegram_bot:
          adapter: telegram
          config:
            bot_token: "{{secret.TELEGRAM_BOT_TOKEN}}"
          activation:
            session: "{{event.chat_id}}"
            message: "{{event.text}}"
            reply: auto
          max_concurrent: 5

        slack_bot:
          adapter: slack
          config:
            bot_token: "{{secret.SLACK_BOT_TOKEN}}"
            app_token: "{{secret.SLACK_APP_TOKEN}}"
          activation:
            session: "{{event.channel_id}}"
            message: "{{event.text}}"
            context: "Channel: {{event.channel_name}}, User: {{event.user_name}}"
            reply: auto

        incoming_webhook:
          adapter: webhook
          config:
            path: /hooks/events
            secret: "{{secret.WEBHOOK_SECRET}}"
          activation:
            session: per_event
            message: "New event: {{event.payload}}"
            filter:
              - field: event.payload.status
                equals: completed
            reply: none
```

## Provider config structure

Each provider under `modules.channels.config.providers` has:

| Field | Type | Description |
|-------|------|-------------|
| `adapter` | string | Adapter type: `webhook`, `telegram`, `slack`, `discord`, `email`, `log`, `file_watcher`, `rss`, `queue` |
| `config` | dict | Adapter-specific settings (tokens, URLs, paths) |
| `activation` | object | How inbound events start the agent |
| `enabled` | bool | Whether this provider is active (default: true) |
| `max_concurrent` | int | Max concurrent activations (1-100, default: 5) |

## Activation pipeline (inbound events)

The `activation` block controls how an inbound event becomes an agent conversation:

```yaml
activation:
  agent: main              # Target agent ID (empty = entry_agent)
  session: per_event       # Session strategy: per_event, shared, or template
  message: "{{event.text}}" # User message template with {{event.*}} placeholders
  context: ""              # Extra system prompt context
  expose_data: false       # Expose raw event data to agent context
  reply: auto              # Reply mode: auto, none, explicit
  filter:                  # Drop events that don't match
    - field: event.payload.type
      equals: message
    - field: event.payload.priority
      gt: 3
  prepare:                 # Pre-activation tool calls
    - action: database.fetch_results
      params:
        query: "SELECT * FROM users WHERE id = '{{event.user_id}}'"
      as: user_data
  route:                   # Dynamic agent routing
    field: event.payload.category
    rules:
      - match: billing
        agent: billing_specialist
      - match: technical
        agent: tech_support
      - default: true
        agent: general
```

### Session strategies

| Value | Behavior |
|-------|----------|
| `per_event` | New session per event (stateless) |
| `shared` | All events share one session |
| `{{event.chat_id}}` | Template -- one session per unique value (e.g., per Telegram chat) |

### Reply modes

| Value | Behavior |
|-------|----------|
| `none` | No reply to the source (fire-and-forget) |
| `auto` | Agent's final response is automatically sent back |
| `explicit` | Agent must call `channels.reply()` explicitly |

### Filter conditions

Each filter object checks a dot-path field:

| Key | Description |
|-----|-------------|
| `field` | Dot path to check (e.g., `event.payload.status`) |
| `equals` | Must equal this value |
| `not_equals` | Must not equal this value |
| `contains` | Must contain this substring |
| `gt` | Must be greater than |
| `lt` | Must be less than |

## user_resolver (auto-targeting)

For output channels that need per-user delivery (SMS, email), the `user_resolver` auto-resolves the recipient from a data source:

```yaml
channels:
  sms_user:
    type: sms
    config:
      from_number: "+33600000000"
    user_resolver:
      module: database
      action: fetch_results
      params:
        query: "SELECT phone, email FROM users WHERE session_id = :session_id"
      mapping:
        to_number: phone
        to_address: email
      cache_ttl: 300
```

The resolver runs at delivery time, uses the `session_id` to identify the user, queries the data source, and maps result columns to channel config fields.

## Output channel types (top-level channels:)

| Type | Config keys | Use case |
|------|-------------|----------|
| `webhook` | `url`, `method`, `headers` | Slack incoming webhook, generic HTTP |
| `log` | `level`, `format` | Debug logging |
| `llm_notification` | (none) | Built-in -- delivers to the agent as a system message |
| `gmail` | `to_address`, `from_name`, `subject_template` | Email via Gmail |
| `telegram` | `bot_token`, `chat_id` | Telegram messages |
| `slack` | `webhook_url` or `bot_token`, `channel` | Slack messages |
| `sms` | `account_sid`, `auth_token`, `from_number`, `to_number` | Twilio SMS |
| `kafka` | `bootstrap_servers`, `topic` | Kafka producer |

## Agent tools for channels

The channels module exposes these actions to agents:

- `channels.send_message(provider, message, metadata)` -- send a message through a named provider
- `channels.reply(message)` -- reply to the inbound event that activated this session
- `channels.broadcast(message, providers)` -- send to multiple providers at once
- `channels.list_providers()` -- list available providers
- `channels.provider_status(provider)` -- check a provider's status
- `channels.stats()` -- global channel statistics

## Examples

### Telegram bot with per-chat sessions

```yaml
modules:
  channels:
    config:
      providers:
        telegram:
          adapter: telegram
          config:
            bot_token: "{{secret.TELEGRAM_BOT_TOKEN}}"
          activation:
            session: "{{event.chat_id}}"
            message: "{{event.text}}"
            reply: auto

execution:
  mode: background
  session_mode: multi
```

### Webhook receiver with filtering and preparation

```yaml
modules:
  channels:
    config:
      providers:
        github_webhook:
          adapter: webhook
          config:
            path: /hooks/github
            secret: "{{secret.GITHUB_WEBHOOK_SECRET}}"
          activation:
            session: per_event
            message: "GitHub event: {{event.payload.action}} on {{event.payload.repository.full_name}}"
            filter:
              - field: event.headers.X-GitHub-Event
                equals: pull_request
              - field: event.payload.action
                equals: opened
            prepare:
              - action: database.fetch_results
                params:
                  query: "SELECT config FROM repos WHERE name = '{{event.payload.repository.name}}'"
                as: repo_config
            reply: none
```

### Output channels for scheduler

```yaml
channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{secret.SLACK_WEBHOOK}}"

execution:
  default_channel: slack_alerts
  scheduler: true
  watchers: true
```
