---
id: triggers
---

# Background Mode & Triggers

Background mode lets your app run autonomously - reacting to events instead of
waiting for user input. The app deploys, starts listening, and activates its
agent when a trigger fires.

## Execution Modes

| Mode | Description |
|------|-------------|
| `one_shot` | Single input → single output → exit |
| `conversation` | Multi-turn interactive chat (default) |
| `background` | Autonomous - agent activates on triggers |
| `pipeline` | Chain multiple apps in sequence |

## Background Mode

```yaml
execution:
  mode: background
  max_turns: 30
  timeout: 120
  triggers:
    - id: my-trigger
      type: cron | watch | http
      message: "Template for the agent: {{event.*}}"
```
When deployed, background apps **auto-start** their triggers. The daemon
manages the lifecycle - triggers survive the connection and run until the
app is undeployed.

## When you need a session payload

Triggers split into two families that change how you should think about
**user input**:

| Family | Triggers | Where the input comes from |
|--------|----------|----------------------------|
| **Conversational** | `telegram`, `discord`, `slack`, `email`, `webhook`, `voice` | Each event already carries a real user message - the agent reads `{{event.message}}` and replies on the same channel |
| **Scheduled** | `cron`, `watch`, `rss`, `queue` | The tick fires with **no message** - without extra context, every user gets the same generic activation |

For the **scheduled** family, the right pattern is a **session payload** - a
prompt + metadata + uploaded files the user pre-fills once, that the daemon
replays into every tick as if the user had typed it live. See
[Background Sessions → Session Payload](38-background-sessions.md#session-payload-pre-filled-user-input).

You can also declare a **payload schema** in the YAML so the dashboard renders
a typed form and the daemon refuses to fire on incomplete sessions:

```yaml
execution:
  mode: background
  triggers:
    - id: hourly
      type: cron
      schedule: "0 * * * *"
  payload_schema:
    required: true
    prompt: { required: true, min_length: 20 }
    metadata:
      - name: location
        type: string
        required: true
    files:
      - name: cv
        required: true
        mime: [application/pdf]
        max_size_mb: 5
```
For conversational triggers a payload schema is **optional** - it can still be
useful as a place for persistent preferences ("respond in French", "you are the
support bot for team X"), but it's not required for the agent to do its job.

## Trigger Types

### Cron Trigger

Run the agent on a schedule using standard cron expressions.

```yaml
execution:
  mode: background
  triggers:
    - id: health-check
      type: cron
      schedule: "*/30 * * * *"        # Every 30 minutes
      message: "Run system health check and report issues."

    - id: daily-report
      type: cron
      schedule: "0 9 * * 1-5"        # Weekdays at 9 AM
      message: "Generate daily project status report."
```
**Cron syntax:**
```
 ┌─ minute (0-59)
 │ ┌─ hour (0-23)
 │ │ ┌─ day of month (1-31)
 │ │ │ ┌─ month (1-12)
 │ │ │ │ ┌─ day of week (0-6, Sun=0)
 * * * * *
```

Examples:
- `"*/5 * * * *"` - Every 5 minutes
- `"0 */6 * * *"` - Every 6 hours
- `"30 8 * * 1"` - Monday at 8:30 AM
- `"0 0 1 * *"` - First of every month

Uses `croniter` for precise scheduling if installed, falls back to minute-step
scanning otherwise.

### Watch Trigger

React to new files matching glob patterns.

```yaml
execution:
  mode: background
  triggers:
    - id: new-csv
      type: watch
      paths:
        - "./inbox/*.csv"
        - "./uploads/**/*.xlsx"
      message: "New file detected: {{event.path}}. Process and analyze it."
```
**Behavior:**
- Polls every 5 seconds (configurable)
- Seeds existing files at startup (no false triggers)
- Deduplicates at 10,000 files
- `{{event.path}}` is replaced with the actual file path

### HTTP Trigger

Listen for incoming HTTP requests (webhooks, API calls).

```yaml
execution:
  mode: background
  triggers:
    - id: github-push
      type: http
      path: /hooks/github
      method: POST
      port: 9100                      # Listener port (default: 9100)
      message: "GitHub event: {{event.body}}"
```
**How it works:**
1. The daemon starts an HTTP listener on the specified port and path
2. When a request arrives, the body is injected into the message template
3. The agent is activated with the formatted message

**Template variables:**
| Variable | Description |
|----------|-------------|
| `{{event.body}}` | Request body (truncated to 10KB) |
| `{{event.path}}` | Request URL path |
| `{{event.method}}` | HTTP method (POST, PUT, etc.) |
| `{{event.header.X-GitHub-Event}}` | Specific header value |

**Example - GitHub webhook:**
```yaml
app:
  app_id: github-reviewer
  name: "PR Auto-Reviewer"

modules:
  web: {}
  filesystem: {}
  memory: {}

execution:
  mode: background
  max_turns: 50
  timeout: 300
  triggers:
    - id: pr-opened
      type: http
      path: /hooks/github
      method: POST
      port: 9100
      message: |
        A new GitHub event was received:
        {{event.body}}
        
        If this is a pull_request event with action "opened",
        review the PR and post a comment with your analysis.

agents:
  - id: main
    role: worker
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    system_prompt: |
      You are an automated PR reviewer. When given a GitHub webhook payload,
      analyze the changes and provide constructive feedback.

capabilities:
  default_policy: auto
  grant:
    - module: web
    - module: filesystem
      actions: [read, grep, glob]
    - module: memory
```
> **For advanced webhook handling** (HMAC auth, payload validation, rate
> limiting), use the `channels` module with the webhook adapter instead.
> See [Channels](40-channels.md).

## Routing

By default, a trigger activation creates a **broadcast** session - a single shared
session for the app. For multi-user or multi-session scenarios, triggers support
three routing modes:

| Mode | Description |
|------|-------------|
| `broadcast` | Single shared session (default). All activations go to the same agent. |
| `user` | One session per user. `routing_key` identifies the user. |
| `session` | One session per unique key. `routing_key` can be any template. |

```yaml
execution:
  mode: background
  triggers:
    - id: user-alert
      type: http
      path: /alerts
      routing: user
      routing_key: "{{event.header.X-User-Id}}"
      message: "Alert for user: {{event.body}}"

    - id: per-repo
      type: http
      path: /hooks/github
      routing: session
      routing_key: "{{event.header.X-GitHub-Repo}}"
      message: "GitHub event: {{event.body}}"
```
The `routing_key` supports the same `{{event.*}}` template variables as `message`.
When `routing` is `user` or `session`, `routing_key` is **required** - the
compiler rejects triggers without it.

## Throttling - max_concurrent_activations

Background apps can limit how many activations run at the same time:

```yaml
execution:
  mode: background
  max_concurrent_activations: 5    # Default: 20
  triggers:
    - id: webhook
      type: http
      path: /process
      message: "Process: {{event.body}}"
```
When the limit is reached, new activations are queued and executed as slots
become available. This prevents resource exhaustion from burst traffic.

## HTTP Trigger Implementation

The HTTP trigger listener uses **aiohttp** when available, falling back to a
basic asyncio TCP server when aiohttp is not installed.

| Backend | Behavior |
|---------|----------|
| **aiohttp** (preferred) | Full HTTP server with proper routing, headers, content-type handling. Uses `web.TCPSite` on `127.0.0.1`. |
| **asyncio TCP** (fallback) | Minimal raw socket handler. Parses HTTP requests manually. No HTTPS, no chunked encoding. |

Install aiohttp for production use: `pip install aiohttp`.

## Trigger Fields Reference

| Field | Type | Default | Required | Description |
|-------|------|---------|:---:|-------------|
| `id` | string | - | yes | Unique trigger identifier |
| `type` | string | - | yes | `cron`, `watch`, or `http` |
| `schedule` | string | `""` | cron only | Cron expression (5 fields) |
| `paths` | list[string] | `[]` | watch only | Glob patterns to monitor |
| `path` | string | `""` | http only | HTTP endpoint path |
| `method` | string | `"POST"` | no | HTTP method |
| `port` | int | `9100` | no | HTTP listener port (1024-65535) |
| `message` | string | `""` | no | Message template with `{{event.*}}` |
| `routing` | string | `"broadcast"` | no | Routing mode: `broadcast`, `user`, or `session` |
| `routing_key` | string | `""` | when routing is `user`/`session` | Template for session routing key |

## Channels Module (Advanced)

For production use with multiple trigger types, authentication, and
bidirectional communication, use the `channels` module instead of
`execution.triggers`. It supports:

- **Cron** - Same as trigger but with channels pipeline
- **File watcher** - With payload metadata (size, timestamp)
- **Webhook** - HMAC-SHA256 / API-key auth, payload validation
- **Email** - IMAP/POP3 inbound
- **RSS** - Feed polling and filtering
- **Queue** - SQS/Redis consumer
- **Slack / Discord / Telegram** - Bidirectional messaging

See [Channels](40-channels.md) for configuration.

## Deployment & Lifecycle

### Deploy a background app

**CLI:**
```bash
digitorn app run my-background-app.yaml
```

**API:**
```
POST /api/apps/deploy
{"yaml_path": "/path/to/app.yaml", "force": true}
```

Background apps auto-start their triggers immediately after deployment.

### Monitor triggers

**API:**
```
GET /api/apps/{app_id}/triggers
```

Returns:
```json
{
  "app_id": "github-reviewer",
  "mode": "background",
  "is_background": true,
  "triggers": [
    {
      "id": "pr-opened",
      "type": "http",
      "path": "/hooks/github",
      "method": "POST"
    }
  ],
  "channels": [],
  "scheduled_jobs": [],
  "watchers": []
}
```

### Stop a background app

```bash
digitorn app undeploy github-reviewer
```

Or via API:
```
DELETE /api/apps/github-reviewer
```

## Compiler Validation

The compiler validates triggers at compile time:

- Background mode requires at least one trigger (or the `channels` module)
- Duplicate trigger IDs are rejected
- Type must be `cron`, `watch`, or `http`
- Cron triggers require `schedule`
- Watch triggers require `paths`
- HTTP triggers require `path`
- Port must be between 1024 and 65535
- `routing` must be `broadcast`, `user`, or `session`
- `routing_key` is required when `routing` is `user` or `session`

## Multiple Triggers

An app can have multiple triggers that activate the same agent:

```yaml
execution:
  mode: background
  triggers:
    # Check health every 30 minutes
    - id: scheduled-check
      type: cron
      schedule: "*/30 * * * *"
      message: "Scheduled health check."

    # React to new log files
    - id: new-logs
      type: watch
      paths: ["./logs/*.log"]
      message: "New log file: {{event.path}}. Scan for errors."

    # Receive alerts via webhook
    - id: alert-webhook
      type: http
      path: /alerts
      port: 9200
      message: "Alert received: {{event.body}}"
```
All triggers run concurrently. Each activation creates an independent agent
turn with its own context.
