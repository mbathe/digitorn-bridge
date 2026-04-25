---
id: context_builder-watch-start
title: "context_builder.watch_start (WatchStart)"
type: module-action
module: context_builder
action: watch_start
fqn: context_builder.watch_start
short_name: WatchStart
keywords: [context_builder, watch_start, watchstart, watcher, monitoring, primitive, surveiller, monitorer, watch, observer]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# context_builder.watch_start (WatchStart)

## Description
Start a persistent watcher that periodically executes a tool and reports back ONLY when something interesting happens. Use this to monitor APIs, track process progress, observe file changes, watch database metrics, etc. The watcher runs in the background and does NOT block the conversation — you can keep chatting normally. Notifications are smart: 'on_change' only notifies when the result differs from the previous check, 'on_error' only on errors, 'on_threshold' when a condition is met, 'summary' batches N checks.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | — | Fully qualified tool name to call periodically (module.action). |
| `params` | object |  | — | Parameters for each check invocation. |
| `interval` | number |  | `30.0` | Seconds between checks (min 5, max 3600). |
| `label` | string |  | `` | Human-readable description of what is being monitored. |
| `max_checks` | integer |  | `0` | Maximum number of checks before auto-stopping. 0 = unlimited (default). Use 1 for a one-shot delayed action (timer/reminder). |
| `notify_when` | string |  | `on_change` | When to notify the LLM. One of: 'on_change' (result differs from previous — default), 'on_error' (only on errors or recovery), 'on_threshold' (expression evaluates to true), 'summary' (batch N chec... |
| `notify_config` | object |  | — | Extra config for the notify strategy. For 'on_threshold': {"expression": "result.status_code != 200"}. For 'summary': {"batch_size": 10}. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [watch_start]
```

## Aliases
`surveiller`, `monitorer`, `watch`, `observer`

## Safety
- Risk level: **medium**
