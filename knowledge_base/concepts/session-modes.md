---
id: session-modes
title: "Session modes (background session management)"
type: concept
keywords: [session_mode, mono, multi, per_key, background, session, max_sessions_per_user, max_concurrent_activations, payload, activation, persist]
related: [execution-modes, triggers, payload-schema, channels]
source: packages/digitorn/core/app/schema.py
---

# Session modes -- background session management

## What it is

In `mode: background`, the session mode controls **how many sessions exist per user** and **how triggers route events to sessions**. This determines whether an app behaves like a single shared agent (mono) or a multi-tenant service where each user configures their own instance (multi).

## The session modes

### mono -- one session per user

The default. Each user gets exactly one session, created automatically on first activation. All trigger events for that user go to the same session. The agent remembers context between activations.

```yaml
execution:
  mode: background
  session_mode: mono
```

**Use for:** personal assistants, per-user monitoring, single-purpose automation where each user has one configuration.

**Behavior:**
- Session is auto-created on first trigger event
- All subsequent events go to the same session
- Agent sees full conversation history from previous activations
- No payload_schema needed (the agent uses the system prompt for its task)

### multi -- N sessions per user

Each user can create multiple sessions, each with its own configuration (via payload_schema). Sessions are created via the API with custom parameters. Triggers fire on ALL active sessions for the user (or use routing to target specific ones).

```yaml
execution:
  mode: background
  session_mode: multi
  max_sessions_per_user: 10
```

**Use for:** multi-topic monitoring, per-search-criteria watchers, any app where one user needs multiple independent agents with different configurations.

**Behavior:**
- Sessions are created explicitly via `POST /api/sessions`
- Each session has its own payload (prompt + metadata + files)
- Trigger events fire on all active sessions for the user
- `max_sessions_per_user` limits how many sessions one user can have (default: 10, 0 = unlimited)

## Session templates for channel providers

When using `modules.channels` with inbound adapters, the `activation.session` field controls session creation:

```yaml
activation:
  session: per_event                   # New session per event (stateless)
  session: shared                      # All events share one session
  session: "{{event.chat_id}}"         # One session per unique chat_id
  session: "{{event.header.X-User}}"   # One session per unique user
```

| Value | Behavior |
|-------|----------|
| `per_event` | Creates a new session for every event. Stateless processing. |
| `shared` | All events from all sources go to one shared session. |
| `{{event.X}}` | Template -- one session per unique value. Perfect for Telegram chat_id, Slack channel_id. |

## max_sessions_per_user

Only meaningful in `multi` mode. Limits how many sessions a single user can create.

```yaml
execution:
  session_mode: multi
  max_sessions_per_user: 10    # 0 = unlimited
```

When the limit is reached, new session creation fails with an error. The user must delete old sessions first.

## max_concurrent_activations

Controls how many sessions can be activated in parallel when a broadcast trigger fires. Prevents rate limit storms when thousands of sessions exist.

```yaml
execution:
  max_concurrent_activations: 20   # Default: 20, range: 1-unlimited
```

Activations beyond this limit are queued and processed in order.

## Session persistence

Sessions persist across activations:

1. **Context persists** -- the agent sees the full message history from previous activations (subject to context window limits and compaction).
2. **Memory persists** -- `memory.set_goal()`, `memory.remember()`, and todos carry over.
3. **Payload persists** -- the user's configuration (prompt + metadata + files) stays attached to the session.

## How triggers connect to sessions

When a trigger fires:

1. **broadcast routing** -- the trigger event is sent to ALL active sessions. Each session's agent receives the trigger message and runs independently.
2. **user routing** -- the trigger event is sent to all sessions of the identified user (via `routing_key`).
3. **session routing** -- the trigger event is sent to one specific session (via `routing_key`).

```yaml
triggers:
  - id: cron_check
    type: cron
    schedule: "*/15 * * * *"
    routing: broadcast           # Default: all active sessions
    message: "Run check now"

  - id: user_webhook
    type: http
    path: /hooks/user
    routing: user
    routing_key: "{{event.header.X-User-Id}}"
    message: "User event: {{event.body}}"

  - id: session_webhook
    type: http
    path: /hooks/session
    routing: session
    routing_key: "{{event.header.X-Session-Id}}"
    message: "Session event: {{event.body}}"
```

## Examples

### Mono -- personal email summarizer

```yaml
execution:
  mode: background
  session_mode: mono
  triggers:
    - id: morning
      type: cron
      schedule: "0 8 * * 1-5"
      message: "Summarize today's emails"
```

One agent per user, runs every weekday morning, remembers past summaries.

### Multi -- multi-topic price monitor

```yaml
execution:
  mode: background
  session_mode: multi
  max_sessions_per_user: 20
  triggers:
    - id: check
      type: cron
      schedule: "*/15 * * * *"
      message: "Run the check using your configured criteria"
  payload_schema:
    required: true
    prompt:
      required: true
      label: "What should I monitor?"
      placeholder: "Track BTC price on Binance"
    metadata:
      - name: threshold
        type: number
        label: "Alert threshold (%)"
        default: 5
      - name: notify_on
        type: select
        options: ["increase", "decrease", "both"]
        default: "both"
```

Each user creates multiple monitoring sessions with different criteria. Every 15 minutes, ALL active sessions run their check.

### Channel-based with per-chat sessions

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

Each Telegram chat gets its own persistent session. The agent remembers the conversation with each user.
