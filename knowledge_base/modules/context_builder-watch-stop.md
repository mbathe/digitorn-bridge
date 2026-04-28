---
id: context_builder-watch-stop
title: "context_builder.watch_stop (WatchStop)"
type: module-action
module: context_builder
action: watch_stop
fqn: context_builder.watch_stop
short_name: WatchStop
keywords: [context_builder, watch_stop, watchstop, watcher, primitive, arreter_surveillance, stop_watch]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.watch_stop (WatchStop)

## Description
Stop and remove a watcher. The watcher is cancelled and its history is discarded.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `watcher_id` | string | ✓ | - | Watcher ID returned by watch_start. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [watch_stop]
```

## Aliases
`arreter_surveillance`, `stop_watch`

## Safety
- Risk level: **low**
