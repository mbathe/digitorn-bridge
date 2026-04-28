---
id: channels-simulate-event
title: "channels.simulate_event (ChannelsSimulateEvent)"
type: module-action
module: channels
action: simulate_event
fqn: channels.simulate_event
short_name: ChannelsSimulateEvent
keywords: [channels, simulate_event, channelssimulateevent, debug]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# channels.simulate_event (ChannelsSimulateEvent)

## Description
Simulate an inbound event for testing purposes.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `provider` | string | ✓ | - | Provider instance to simulate on. |
| `payload` | object |  | - | Simulated event payload. |
| `source` | string |  | `test` | Simulated sender identifier. |
| `message` | string |  | `` | Simulated message text. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: channels
      actions: [simulate_event]
```

## Safety
- Risk level: **medium**
