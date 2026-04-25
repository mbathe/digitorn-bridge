---
id: context_builder-watch-history
title: "context_builder.watch_history (WatchHistory)"
type: module-action
module: context_builder
action: watch_history
fqn: context_builder.watch_history
short_name: WatchHistory
keywords: [context_builder, watch_history, watchhistory, watcher, history, primitive, historique_surveillance, watch_checks]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.watch_history (WatchHistory)

## Description
Get the last N check results from a watcher's history. Each entry includes timestamp, result/error, and whether a notification was triggered.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `watcher_id` | string | ✓ | — | Watcher ID returned by watch_start. |
| `last_n` | integer |  | `10` | Number of recent check results to return. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [watch_history]
```

## Aliases
`historique_surveillance`, `watch_checks`

## Safety
- Risk level: **low**
