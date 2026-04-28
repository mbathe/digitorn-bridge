---
id: channels-pause-provider
title: "channels.pause_provider (ChannelsPauseProvider)"
type: module-action
module: channels
action: pause_provider
fqn: channels.pause_provider
short_name: ChannelsPauseProvider
keywords: [channels, pause_provider, channelspauseprovider, admin, pause_canal]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# channels.pause_provider (ChannelsPauseProvider)

## Description
Pause a provider's inbound listener.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider` | string | ✓ | - | Provider instance name to pause. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [pause_provider]
```

## Aliases
`pause_canal`

## Safety
- Risk level: **medium**
