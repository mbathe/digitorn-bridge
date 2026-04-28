---
id: context_builder-watch-pause
title: "context_builder.watch_pause (WatchPause)"
type: module-action
module: context_builder
action: watch_pause
fqn: context_builder.watch_pause
short_name: WatchPause
keywords: [context_builder, watch_pause, watchpause, watcher, primitive, pause_surveillance, pause_watch]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.watch_pause (WatchPause)

## Description
Pause a running watcher. The timer continues but checks are skipped. History is preserved. Use watch_resume to restart.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `watcher_id` | string | ✓ | - | Watcher ID returned by watch_start. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [watch_pause]
```

## Aliases
`pause_surveillance`, `pause_watch`

## Safety
- Risk level: **low**
