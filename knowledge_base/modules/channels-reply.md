---
id: channels-reply
title: "channels.reply (ChannelsReply)"
type: module-action
module: channels
action: reply
fqn: channels.reply
short_name: ChannelsReply
keywords: [channels, reply, channelsreply, repondre, reply_channel]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# channels.reply (ChannelsReply)

## Description
Reply to the current inbound event on its originating channel.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `text` | string | ✓ | - | Reply text. |
| `metadata` | object |  | - | Extra metadata for the reply. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [reply]
```

## Aliases
`repondre`, `reply_channel`

## Safety
- Risk level: **medium**
