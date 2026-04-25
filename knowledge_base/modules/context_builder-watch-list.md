---
id: context_builder-watch-list
title: "context_builder.watch_list (WatchList)"
type: module-action
module: context_builder
action: watch_list
fqn: context_builder.watch_list
short_name: WatchList
keywords: [context_builder, watch_list, watchlist, watcher, list, primitive, liste_surveillances, list_watches]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.watch_list (WatchList)

## Description
List all watchers with their current status, check counts, and notification counts. Running watchers are shown first.

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [watch_list]
```

## Aliases
`liste_surveillances`, `list_watches`

## Safety
- Risk level: **low**
