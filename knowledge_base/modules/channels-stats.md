---
id: channels-stats
title: "channels.stats (ChannelsStats)"
type: module-action
module: channels
action: stats
fqn: channels.stats
short_name: ChannelsStats
keywords: [channels, stats, channelsstats, info, stats_canaux]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# channels.stats (ChannelsStats)

## Description
Get aggregate statistics for all channel providers.

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [stats]
```

## Aliases
`stats_canaux`

## Safety
- Risk level: **low**
