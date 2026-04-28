---
id: triggers
title: "Triggers (execution triggers for background apps)"
type: concept
keywords: [trigger, cron, watch, http, schedule, file_watcher, webhook, routing, broadcast, user, session, routing_key, background, activation]
related: [execution-modes, session-modes, channels, payload-schema]
source: packages/digitorn/core/app/schema.py
---

# Triggers -- execution triggers for background apps

## What it is

Triggers define **when** a background-mode app activates and runs its agent. They live under `execution.triggers` and fire events that start agent conversations. Three types exist: **cron** (time-based), **watch** (file system), and **http** (incoming HTTP requests).

Triggers are only meaningful in `mode: background`. Conversation and one_shot modes do not use triggers.

## YAML reference

```yaml
execution:
  mode: background
  triggers:
    - id: <unique-id>
      type: cron | watch | http
      # type-specific fields below
      message: "Template with {{event.*}} variables"
      routing: broadcast | user | session
      routing_key: "{{event.header.X-User-Id}}"
```

### Trigger fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique trigger identifier |
| `type` | string | yes | `cron`, `watch`, or `http` |
| `schedule` | string | cron only | Cron expression (e.g., `*/15 * * * *`) |
| `paths` | list[string] | watch only | Glob patterns to watch (e.g., `["./inbox/*.csv"]`) |
| `path` | string | http only | HTTP endpoint path (e.g., `/hooks/ingest`) |
| `method` | string | http only | HTTP method (default: `POST`) |
| `port` | int | http only | Port for HTTP listener (1024-65535, default: 9100) |
| `message` | string | no | Message template sent to the agent. Supports `{{event.*}}` |
| `routing` | string | no | Routing mode: `broadcast`, `user`, `session` (default: `broadcast`) |
| `routing_key` | string | no | Template to extract routing identifier from the event |

## Trigger types

### cron -- time-based schedules

Fires at regular intervals using standard cron syntax.

```yaml
triggers:
  - id: hourly_check
    type: cron
    schedule: "0 * * * *"          # Every hour
    message: "Run the hourly check"

  - id: weekday_morning
    type: cron
    schedule: "0 8 * * 1-5"        # Mon-Fri at 8am
    message: "Good morning. Run the daily report."

  - id: every_15_min
    type: cron
    schedule: "*/15 * * * *"       # Every 15 minutes
    message: "Check for updates"
```

Common cron patterns:
- `*/5 * * * *` -- every 5 minutes
- `0 * * * *` -- every hour
- `0 8 * * *` -- daily at 8am
- `0 8 * * 1-5` -- weekdays at 8am
- `0 0 * * 0` -- weekly on Sunday at midnight
- `0 0 1 * *` -- monthly on the 1st at midnight

### watch -- file system changes

Fires when files matching glob patterns are created or modified.

```yaml
triggers:
  - id: new_csv
    type: watch
    paths:
      - "./inbox/*.csv"
      - "./inbox/*.xlsx"
    message: "New file detected: {{event.path}}"
```

Available `{{event.*}}` variables for watch:
- `{{event.path}}` -- full path of the changed file
- `{{event.name}}` -- file name only
- `{{event.type}}` -- event type (created, modified, deleted)

### http -- incoming HTTP requests

Fires when an HTTP request hits the specified endpoint.

```yaml
triggers:
  - id: webhook_ingest
    type: http
    path: /hooks/ingest
    method: POST
    port: 9100
    message: "Webhook received: {{event.body}}"
    routing: session
    routing_key: "{{event.header.X-Session-Id}}"
```

Available `{{event.*}}` variables for http:
- `{{event.body}}` -- request body (string)
- `{{event.payload}}` -- parsed JSON body (dict)
- `{{event.header.X-Name}}` -- request header value
- `{{event.query.param}}` -- query string parameter
- `{{event.method}}` -- HTTP method
- `{{event.path}}` -- request path

## Routing modes

Routing controls **which sessions** receive the trigger event.

### broadcast (default)

The event is sent to **all active sessions** for this app. Used for shared updates, global notifications.

```yaml
triggers:
  - id: global_update
    type: cron
    schedule: "0 * * * *"
    routing: broadcast
    message: "Hourly update for all users"
```

### user

The event is sent to **all sessions of a specific user**, identified by `routing_key`.

```yaml
triggers:
  - id: user_webhook
    type: http
    path: /hooks/user-event
    routing: user
    routing_key: "{{event.header.X-User-Id}}"
    message: "Event for user: {{event.body}}"
```

### session

The event is sent to **one specific session**, identified by `routing_key`.

```yaml
triggers:
  - id: session_webhook
    type: http
    path: /hooks/session-event
    routing: session
    routing_key: "{{event.header.X-Session-Id}}"
    message: "Session-specific event: {{event.body}}"
```

## Concurrency control

When a broadcast trigger fires and there are thousands of sessions:

```yaml
execution:
  max_concurrent_activations: 20   # Max parallel LLM calls (default: 20)
```

Activations beyond this limit are queued and processed in order.

## Examples

### Scheduled monitor (template 01 pattern)

```yaml
execution:
  mode: background
  session_mode: multi
  max_turns: 15
  timeout: 120
  triggers:
    - id: scheduled_check
      type: cron
      schedule: "*/15 * * * *"
      message: |
        Run the monitoring check now.
        Use the session payload for configuration.
  payload_schema:
    required: true
    prompt:
      required: true
      label: "What should I monitor?"
      placeholder: "Check the price of BTC on Binance"
    metadata:
      - name: frequency
        type: select
        options: ["every 15 minutes", "every hour", "daily"]
        default: "every hour"

channels:
  slack_alerts:
    type: webhook
    config:
      url: "{{secret.SLACK_WEBHOOK}}"
```

### Event webhook processor (template 03 pattern)

```yaml
execution:
  mode: background
  session_mode: mono
  max_turns: 20
  timeout: 180
  triggers:
    - id: github_pr
      type: http
      path: /hooks/github
      method: POST
      message: |
        Process this GitHub event:
        Event: {{event.header.X-GitHub-Event}}
        Action: {{event.payload.action}}
        Repo: {{event.payload.repository.full_name}}
        PR: #{{event.payload.number}} - {{event.payload.pull_request.title}}
      routing: session
      routing_key: "{{event.payload.repository.full_name}}"

    - id: periodic_cleanup
      type: cron
      schedule: "0 2 * * *"
      message: "Run nightly cleanup of stale reviews"
```

### File watcher pipeline

```yaml
execution:
  mode: background
  triggers:
    - id: new_document
      type: watch
      paths:
        - "./inbox/*.pdf"
        - "./inbox/*.docx"
      message: "New document to process: {{event.path}}"
      routing: broadcast
```
