---

id: triggers
title: Triggers
sidebar_position: 10
format: md
---


# Triggers

Triggers define how an app is started. An app can have multiple triggers.

## Trigger Types

| Type | Description |
|------|-------------|
| `cli` | Interactive terminal input |
| `http` | HTTP endpoint |
| `webhook` | Incoming webhook with authentication |
| `schedule` | Cron-based scheduling |
| `watch` | File system change watcher |
| `event` | Event bus subscription |

## CLI Trigger

The most common trigger for interactive use:

```yaml
triggers:
  - type: cli
    mode: conversation             # conversation | one_shot
    prompt: "> "                   # Input prompt string
    multiline: true                # Allow multi-line input
    history: true                  # Enable input history
    greeting: |                    # Message shown on startup
      My App v1.0
      Workspace: {{workspace}}
      Type your request. Press Ctrl+C to exit.
```

### CLI Modes

**Conversation mode** - Multi-turn interaction. The agent maintains context across messages:

```yaml
triggers:
  - type: cli
    mode: conversation
    greeting: "Hello! Ask me anything."
```

```
$ llmos app run my-app.app.yaml
Hello! Ask me anything.
> Read the README
[agent reads and responds]
> Now summarize it
[agent has context from previous turn]
> /clear
[conversation reset]
```

**One-shot mode** - Single input, single response, then exit:

```yaml
triggers:
  - type: cli
    mode: one_shot
    greeting: "Enter your query:"
```

```
$ llmos app run my-app.app.yaml
Enter your query:
> What's in the src directory?
[agent responds and exits]
```

Or pass input directly:

```bash
llmos app run my-app.app.yaml --input "List files in src/"
```

## HTTP Trigger

Expose the app as an HTTP endpoint:

```yaml
triggers:
  - type: http
    path: /review                  # URL path
    method: POST                   # HTTP method (default: POST)
```

When the daemon is running, this creates an endpoint at `POST /apps/{app_id}/run`.

### HTTP with Authentication

```yaml
triggers:
  - type: http
    path: /analyze
    method: POST
    auth:
      type: bearer                 # bearer | api_key | hmac | none
      header: "Authorization"
```

### HTTP Response Format

```yaml
triggers:
  - type: http
    path: /api
    response:
      format: json                 # json | streaming_json | sse
```

## Webhook Trigger

Receive events from external services:

```yaml
triggers:
  - type: webhook
    path: /github-webhook
    auth:
      type: hmac
      secret: "{{secret.GITHUB_WEBHOOK_SECRET}}"
      header: "X-Hub-Signature-256"
    events: [push, pull_request]   # Filter by event type
    transform: |
      Review PR #{{trigger.body.pull_request.number}}:
      {{trigger.body.pull_request.title}}
    filters:
      - "{{trigger.body.action == 'opened'}}"
```

### Webhook Authentication Types

| Type | Description |
|------|-------------|
| `none` | No authentication |
| `bearer` | Bearer token in header |
| `api_key` | API key in header |
| `hmac` | HMAC signature verification |

### Webhook Fields

| Field | Type | Description |
|-------|------|-------------|
| `path` | string | URL path for the webhook |
| `auth` | object | Authentication configuration |
| `events` | list | Event types to accept |
| `body` | dict | Expected body schema |
| `response` | dict | Response configuration |
| `transform` | string | Template to transform payload to input |
| `filters` | list | Filter expressions (must all be true) |

## Schedule Trigger

Run the app on a schedule:

```yaml
triggers:
  - type: schedule
    cron: "0 9 * * 1-5"           # Every weekday at 9 AM
    timezone: "America/New_York"
    input: "Generate daily report"

  - type: schedule
    when: "every 30 minutes"       # Natural language schedule
    input: "Check system health"
```

### Cron Syntax

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-6, Sun=0)
│ │ │ │ │
* * * * *
```

Examples:
- `"0 */6 * * *"` - Every 6 hours
- `"30 8 * * 1"` - Monday at 8:30 AM
- `"0 0 1 * *"` - First of every month at midnight

## Watch Trigger

React to file system changes:

```yaml
triggers:
  - type: watch
    paths:
      - "{{workspace}}/src/**/*.py"
      - "{{workspace}}/tests/**/*.py"
    debounce: "2s"                 # Wait before triggering (default: 2s)
    transform: "File changed: {{trigger.path}}"
```

## Event Trigger

React to events from the LLMOS event bus:

```yaml
triggers:
  - type: event
    topic: "llmos.modules.installed"
    transform: "New module installed: {{trigger.event.module_id}}"
```

## Common Fields

These fields work on all trigger types:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique trigger identifier |
| `transform` | string | Template to transform the trigger payload into input |
| `filters` | list | Expression filters (all must be true to trigger) |
| `input` | string | Static input text (for schedule triggers) |

## Trigger Context

Inside templates, access trigger data with `{{trigger.*}}`:

```yaml
agent:
  system_prompt: |
    User input: {{trigger.input}}
    Trigger type: {{trigger.type}}

flow:
  - agent: default
    input: "Process: {{trigger.input}}"
```

### Available Trigger Data

| Path | Description |
|------|-------------|
| `trigger.input` | The input text/payload |
| `trigger.type` | Trigger type (cli, http, etc.) |
| `trigger.body` | HTTP/webhook request body |
| `trigger.headers` | HTTP headers |
| `trigger.path` | Watch: changed file path |
| `trigger.event` | Event: event payload |

## Trigger Categories

LLMOS triggers fall into two categories:

### Entry-Point Triggers (External → App)

These triggers define how an app is **invoked** by a user or external service:

| Type | Source | Handled By |
| ---- | ------ | ---------- |
| `cli` | Terminal user input | CLI REPL |
| `http` | HTTP API request | FastAPI server |
| `webhook` | External service (GitHub, Slack) | FastAPI server |

Entry-point triggers are always handled by their respective servers. They are NOT registered with the daemon's TriggerDaemon.

### Background Triggers (Daemon-Managed)

These triggers run continuously and **start the app** when conditions are met:

| Type | Source | Daemon Infrastructure |
| ---- | ------ | --------------------- |
| `schedule` | Cron/interval timer | `CronWatcher` (croniter) or `IntervalWatcher` |
| `watch` | File system changes | `FileSystemWatcher` (watchfiles/inotify) |
| `event` | EventBus messages | EventBus subscription |

When the daemon is running, background triggers are delegated to the **TriggerDaemon** via the `AppTriggerBridge`. This provides:

- **Real cron scheduling** via `croniter` (not approximate sleep loops)
- **Real filesystem watching** via `watchfiles` (inotify, not polling)
- **Priority scheduling** with configurable concurrency limits
- **Throttling** - max fires per hour, minimum interval between fires
- **Conflict resolution** - queue, preempt, or reject duplicate fires
- **Health monitoring** - crashed watchers are detected and reported
- **Persistence** - triggers survive daemon restarts (SQLite-backed)

Without the daemon, a lightweight `TriggerManager` provides basic standalone support.

## Daemon Mode: Background Triggers

When an app is **registered** with the daemon and its status is set to `"running"`, background triggers (`schedule`, `watch`, `event`) are **automatically started** by the daemon. You don't need to run the CLI - the daemon manages trigger lifecycles.

### How It Works

1. Register your app: `llmos app register my-app.app.yaml` (or `POST /apps/register`)
2. Set status to running: `PUT /apps/{id}/status` with `{"status": "running"}`
3. The `AppTriggerBridge` converts YAML triggers to daemon `TriggerDefinition` objects
4. The `TriggerDaemon` creates appropriate watchers (CronWatcher, FileSystemWatcher, etc.)
5. When a watcher fires, the daemon invokes `AppRuntime.run()` for the app
6. When status changes to `"stopped"`, triggers are deactivated and deleted

```yaml
# This app runs a health check every 30 minutes - no CLI needed
app:
  name: health-monitor
  version: "1.0"

triggers:
  - type: schedule
    when: "every 30 minutes"
    input: "Check system health and report any issues"

  - type: event
    topic: "llmos.modules.installed"
    transform: "New module installed: {{trigger.event.module_id}}. Verify it works."

agent:
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
  system_prompt: "You are a system health monitor."
  tools:
    - module: os_exec
      action: run_command

security:
  profile: local_worker
```

After registering and setting to "running", the daemon will:

- Execute the health check every 30 minutes
- React whenever a new module is installed via the event bus
- All executions go through the full security pipeline (PermissionGuard, Scanner, Sanitizer, Audit)

### Trigger Lifecycle

| App Status | CLI Trigger | Schedule/Watch/Event |
| ---------- | ----------- | -------------------- |
| `registered` | Manual only | Not started |
| `running` | Manual only | Auto-started by daemon |
| `stopped` | Disabled | Auto-stopped |
| `error` | Disabled | Auto-stopped |

## Multiple Triggers

An app can have multiple triggers. Each defines a different way to start the app:

```yaml
triggers:
  # Interactive CLI
  - type: cli
    mode: conversation
    greeting: "Code Reviewer ready."

  # HTTP API
  - type: http
    path: /review
    method: POST

  # GitHub webhook
  - type: webhook
    path: /github
    auth:
      type: hmac
      secret: "{{secret.GITHUB_SECRET}}"
    events: [pull_request]
    filters:
      - "{{trigger.body.action == 'opened'}}"
    transform: "Review PR: {{trigger.body.pull_request.html_url}}"

  # Daily schedule
  - type: schedule
    cron: "0 9 * * 1-5"
    input: "Run daily code quality check"
```

## Transform Templates

The `transform` field uses the full ExpressionEngine with `{{expression}}` syntax:

```yaml
triggers:
  - type: webhook
    path: /github
    transform: "Review PR #{{trigger.body.pull_request.number}}: {{trigger.body.pull_request.title}}"

  - type: schedule
    cron: "0 9 * * 1-5"
    transform: "Daily report for {{trigger.input}}"

  - type: event
    topic: "llmos.modules.installed"
    transform: "New module: {{trigger.event.module_id}}"
```

Available template variables in transforms:

| Variable | Description |
| -------- | ----------- |
| `{{input}}` | Raw input text |
| `{{payload}}` | Alias for input |
| `{{trigger.input}}` | The trigger input |
| `{{trigger.type}}` | Trigger type (cli, http, etc.) |
| Any metadata key | `{{source}}`, `{{event}}`, etc. |

## Filter Expressions

Filters can be either **glob patterns** or **expression conditions**:

```yaml
triggers:
  # Glob patterns - matched against input text
  - type: cli
    filters:
      - "fix*"
      - "bug*"

  # Expression conditions - evaluated as boolean
  - type: webhook
    path: /github
    filters:
      - "{{trigger.body.action == 'opened'}}"
      - "{{trigger.body.pull_request.draft != true}}"
```

If any filter matches, the trigger fires. If no filters match, the trigger is skipped.

## Compiler Validation

The compiler validates triggers at compile time (step 18):

- **Duplicate HTTP paths** - Two triggers with the same `method:path` are rejected
- **HTTP path format** - Paths must start with `/`
- **Transform brackets** - Mismatched `{{` / `}}` are caught
- **Schedule intervals** - Very short intervals (<10s) produce warnings
- **Wildcard topics** - Subscribing to `*` or `#` produces warnings
- **Required fields** - Each trigger type has required fields (schedule needs `cron`/`when`, watch needs `paths`, event needs `topic`, http needs `path`)
- **Cron format** - Cron expressions must have 5 or 6 fields

## Architecture: AppTriggerBridge

The `AppTriggerBridge` connects YAML app triggers to the daemon's `TriggerDaemon`:

```text
YAML AppDefinition
    └─ triggers:
        ├─ cli / http / webhook     → Handled by CLI REPL / FastAPI (entry points)
        └─ schedule / watch / event → AppTriggerBridge
                                        └─ Converts to daemon TriggerDefinition
                                            └─ TriggerDaemon.register()
                                                ├─ CronWatcher (croniter)
                                                ├─ IntervalWatcher
                                                ├─ FileSystemWatcher (watchfiles)
                                                └─ EventBus subscription
                                                    └─ On fire → AppRuntime.run()
```

### Standalone Fallback

When the daemon is not running (e.g., `llmos app run` without a daemon), the lightweight `TriggerManager` provides basic support:

- Schedule triggers use `asyncio.sleep` loops (approximate intervals)
- Watch triggers use `Path.glob()` polling (not inotify)
- Event triggers subscribe directly to the EventBus

The daemon mode is always preferred for production use.
