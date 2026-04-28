---
id: queue-ack
title: "queue.ack (QueueAck)"
type: module-action
module: queue
action: ack
fqn: queue.ack
short_name: QueueAck
keywords: [queue, ack, queueack, write, confirmer, acknowledge]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# queue.ack (QueueAck)

## Description
Acknowledge messages after successful processing.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | - | Queue name. |
| `message_ids` | array | ✓ | - | List of ack_id values from received messages. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [ack]
```

## Aliases
`confirmer`, `acknowledge`

## Safety
- Risk level: **low**
