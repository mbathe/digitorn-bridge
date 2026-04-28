---
id: queue-receive
title: "queue.receive (QueueReceive)"
type: module-action
module: queue
action: receive
fqn: queue.receive
short_name: QueueReceive
keywords: [queue, receive, queuereceive, read, recevoir, pull, poll]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# queue.receive (QueueReceive)

## Description
Pull messages from a queue (poll mode). Use ack_mode='manual' to acknowledge after processing.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | - | Queue to receive from. |
| `timeout` | number |  | `5.0` | Wait up to N seconds for messages. |
| `batch_size` | integer |  | `1` | Max messages to receive. |
| `ack_mode` | string |  | `manual` | 'auto' = auto-ack on receive, 'manual' = must call ack(). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [receive]
```

## Aliases
`recevoir`, `pull`, `poll`

## Safety
- Risk level: **low**
