---
id: channels-broadcast
title: "channels.broadcast (ChannelsBroadcast)"
type: module-action
module: channels
action: broadcast
fqn: channels.broadcast
short_name: ChannelsBroadcast
keywords: [channels, broadcast, channelsbroadcast, send, diffuser, broadcast_message]
permissions: []
risk_level: high
irreversible: false
require_approval: false
---

# channels.broadcast (ChannelsBroadcast)

## Description
Broadcast a message to multiple channel providers.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `providers` | array | ✓ | - | List of provider instance names. |
| `text` | string | ✓ | - | Message text. |
| `subject` | string |  | `` | Subject/title. |
| `metadata` | object |  | - | Extra metadata. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [broadcast]
```

## Aliases
`diffuser`, `broadcast_message`

## Safety
- Risk level: **high**
