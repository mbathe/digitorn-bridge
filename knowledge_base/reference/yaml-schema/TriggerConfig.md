---
id: yaml-schema-triggerconfig
title: "TriggerConfig — YAML schema reference"
type: schema-reference
model: TriggerConfig
is_root: false
keywords: [triggerconfig, id, message, method, path, paths, port, routing, routing_key, schedule, type]
---

# TriggerConfig

## Description
A trigger for background mode.

Example::

triggers:
- id: new_csv
type: watch
paths: ["./inbox/*.csv"]
message: "New file: {{event.path}}"

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `id` | str | ✓ | — | Unique trigger identifier. |
| `type` | str | ✓ | — | Trigger type: 'cron', 'watch', 'http'. |
| `schedule` | str |  | `''` | Cron expression (cron type only). |
| `paths` | list[str] |  | `[]` | Glob patterns to watch (watch type only). |
| `path` | str |  | `''` | HTTP endpoint path (http type only). |
| `method` | 'GET' \| 'POST' \| 'PUT' \| 'DELETE' \| 'PATCH' \| 'HEAD' \| 'OPTIONS' |  | `'POST'` | HTTP method (http type only). |
| `port` | int |  | `9100` | Port for HTTP trigger listener (default 9100). |
| `message` | str |  | `''` | Message template sent to the agent. Supports {{event.*}}. |
| `routing` | str |  | `'broadcast'` | How this trigger routes to sessions: 'broadcast' (all active sessions), 'user' (all sessions of the identified user), 'session' (one specific session). |
| `routing_key` | str |  | `''` | Template to extract the routing identifier from the event payload. For routing='user': identifies which user (e.g. '{{event.chat_id}}'). For routing='session': identifies which session (e.g. '{{event.header.X-Session-Id}}'). |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
