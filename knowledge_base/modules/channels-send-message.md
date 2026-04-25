---
id: channels-send-message
title: "channels.send_message (ChannelsSendMessage)"
type: module-action
module: channels
action: send_message
fqn: channels.send_message
short_name: ChannelsSendMessage
keywords: [channels, send_message, channelssendmessage, send, envoyer_message, send_on_channel]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# channels.send_message (ChannelsSendMessage)

## Description
Send a message through a specific channel provider.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider` | string | ✓ | — | Provider instance name from the channels config. |
| `text` | string | ✓ | — | Message text to send. |
| `recipient` | string |  | `` | Override recipient (phone, email, channel ID). If empty, uses the provider's default target. |
| `subject` | string |  | `` | Subject/title (email, Slack header). |
| `thread_id` | string |  | `` | Thread ID for reply threading (Slack ts, email Message-ID). |
| `metadata` | object |  | — | Extra metadata passed to the adapter. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [send_message]
```

## Aliases
`envoyer_message`, `send_on_channel`

## Safety
- Risk level: **medium**
