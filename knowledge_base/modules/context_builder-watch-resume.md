---
id: context_builder-watch-resume
title: "context_builder.watch_resume (WatchResume)"
type: module-action
module: context_builder
action: watch_resume
fqn: context_builder.watch_resume
short_name: WatchResume
keywords: [context_builder, watch_resume, watchresume, watcher, primitive, reprendre_surveillance, resume_watch]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.watch_resume (WatchResume)

## Description
Resume a paused watcher. Checks restart immediately.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `watcher_id` | string | ✓ | - | Watcher ID returned by watch_start. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [watch_resume]
```

## Aliases
`reprendre_surveillance`, `resume_watch`

## Safety
- Risk level: **low**
