---
id: channels-test-send
title: "channels.test_send (ChannelsTestSend)"
type: module-action
module: channels
action: test_send
fqn: channels.test_send
short_name: ChannelsTestSend
keywords: [channels, test_send, channelstestsend, debug]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# channels.test_send (ChannelsTestSend)

## Description
Send a test message to verify outbound connectivity.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider` | string | ✓ | - | Provider instance to test. |
| `text` | string |  | `Digitorn test message` | Test message content. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [test_send]
```

## Safety
- Risk level: **medium**
