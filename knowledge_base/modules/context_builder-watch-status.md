---
id: context_builder-watch-status
title: "context_builder.watch_status (WatchStatus)"
type: module-action
module: context_builder
action: watch_status
fqn: context_builder.watch_status
short_name: WatchStatus
keywords: [context_builder, watch_status, watchstatus, watcher, status, primitive, statut_surveillance, watch_info]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.watch_status (WatchStatus)

## Description
Get detailed status of a watcher: metrics, last result, configuration, and recent history.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `watcher_id` | string | ✓ | - | Watcher ID returned by watch_start. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [watch_status]
```

## Aliases
`statut_surveillance`, `watch_info`

## Safety
- Risk level: **low**
