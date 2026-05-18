---
id: tutorial-05-background
title: "5. Background mode"
sidebar_label: "5. Background"
---

The first four tutorials all ran in `runtime.mode: conversation` -
the agent only thinks when a user types something. **Background
mode** flips that: the daemon wakes the agent on its own schedule
or when an external event arrives. No human in the loop.

The smallest background app declares one trigger and a one-line
prompt the trigger uses to wake the agent.

## Prerequisites

Same daemon and credential as the previous tutorials.

## The YAML

Save this as `daily-monitor.yaml`. The app fires on a cron at 09:00
on weekdays; for testing, fire it manually from the API.

```yaml
app:
  app_id: daily-monitor
  name: Daily Monitor
  version: "1.0"

runtime:
  mode: background
  workdir_mode: auto
  max_turns: 4
  timeout: 60
  triggers:
    - id: morning_check
      type: cron
      schedule: "0 9 * * 1-5"        # 09:00 Mon-Fri
      message: "Time for the daily check. Reply with: monitor running."

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      credential:
        ref: deepseek_main
        scope: per_user
        provider: deepseek
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: https://api.deepseek.com/v1
      temperature: 0
      max_tokens: 64
    system_prompt: |
      You are a background monitor. Each time you wake up, reply
      with one short status line. Do not ask questions; the user
      is not online when you fire.

tools:
  modules: {}
  capabilities:
    default_policy: auto
```

Two new things vs the previous tutorials:

- **`runtime.mode: background`** - tells the daemon this app is
  trigger-driven, not chat-driven.
- **`runtime.triggers`** - a list of activation sources. This one
  is a `cron` trigger; other types include `watch` (file system),
  `http` (webhook), and the connectors exposed by the
  [channels module](../language/40-channels.md) (Slack, Telegram,
  email, RSS, queue, …).

## Deploy

```bash
digitorn dev deploy daily-monitor.yaml
```

After deploy, list the registered triggers to confirm they're
armed:

```bash
curl -H "Authorization: Bearer $TOKEN" \
     http://127.0.0.1:8000/api/apps/daily-monitor/triggers
```

The response includes the trigger entry, parsed cron schedule,
and the dispatch route the daemon will use when it fires.

## Fire the trigger manually

Cron at 09:00 Mon-Fri is hard to wait for in a tutorial. The
daemon exposes a manual-fire endpoint that runs the activation
right now:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{}' \
     http://127.0.0.1:8000/api/apps/daily-monitor/triggers/morning_check/fire
```

Response:

```json
{
  "fired": true,
  "trigger_id": "morning_check",
  "trigger_type": "cron",
  "message": "Time for the daily check. Reply with: monitor running.",
  "dispatch": "global_fallback",
  "routing": "broadcast",
  "note": "Manual fire does not create a new background_session; it activates existing ones (or falls back to a global run if none exist)."
}
```

## Live activation result

Poll `GET /api/apps/daily-monitor/activations` to see the run land.
Real result captured against the daemon, ~2 seconds after the
manual fire:

```json
{
  "id": "6d8e4751b8e64cfc9e161b2e71faa91d",
  "app_id": "daily-monitor",
  "trigger_id": "morning_check",
  "trigger_type": "cron",
  "status": "completed",
  "message": "Time for the daily check. Reply with: monitor running.",
  "trigger_payload": { "manual": true },
  "started_at": "2026-05-08 19:38:26.157270+00:00",
  "completed_at": "2026-05-08 19:38:27.418035+00:00",
  "duration_ms": 1260.8,
  "response": "monitor running.",
  "tool_calls_count": 0,
  "turns_used": 1,
  "prompt_tokens": 2078,
  "completion_tokens": 5,
  "error": null
}
```

The agent woke up, used one turn, called no tools, and produced
the five-token reply `"monitor running."` in 1.26 s. The
activation row stays in the database for audit, observability,
and cron-job history.

## Other trigger types

`cron` is the simplest. The same `runtime.triggers` list accepts
several richer types; pick the one that matches your event source:

| Type      | What it watches                       | Example use                          |
|-----------|---------------------------------------|--------------------------------------|
| `cron`    | Time of day / day of week             | Daily digest, hourly poll            |
| `watch`   | A path on disk for file changes       | "When a new PDF lands, ingest it"    |
| `http`    | An incoming HTTP request to a webhook | GitHub webhook handler               |
| `channel` | Any provider in `tools.channels`      | "On new Slack message in #alerts, …" |

The full schema lives in
[Triggers](../language/09-triggers.md). Each type has its own
payload shape; the trigger payload is exposed to the agent's
prompt template as `{{event.payload.X}}`.

## Wiring multiple triggers

A single app can declare several triggers. Each one targets the
same set of agents but provides a different `message` template
and gets its own activation log. Example:

```yaml
runtime:
  mode: background
  triggers:
    - id: morning_check
      type: cron
      schedule: "0 9 * * 1-5"
      message: "Daily 09:00 check. Read the inbox, summarise."

    - id: alert_webhook
      type: http
      path: /alerts
      message: "Alert received: {{event.payload.title}}. Investigate."

    - id: new_pdf
      type: watch
      path: /var/incoming
      message: "New file at {{event.payload.path}}. Ingest into the docs KB."
```

The daemon enforces a per-app `max_concurrent_activations` cap
(default 20) so a burst of webhooks doesn't fan out unbounded.
Per-trigger throttling, retry policy, and dead-letter handling are
documented in [Triggers](../language/09-triggers.md).

## When to use background mode

- The agent does **work without a human** (digests, monitors,
  ingestion pipelines, scheduled reports).
- The work is triggered by **external events** (new file, new
  message, webhook).
- You want **multiple parallel users** each running their own
  background activations - set `runtime.session_mode: multi` and
  the daemon scopes activations per `(user_id, session)` instead
  of broadcasting.

For interactive chat-driven apps, stay on `runtime.mode:
conversation`. Mixed apps can host both - a chat surface plus a
cron trigger that posts a daily summary into the same session.

Next: [6. UI surfaces](06-ui-surfaces.md) - the workspace pane
and declarative widgets.
