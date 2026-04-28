---
id: queue-nack
title: "queue.nack (QueueNack)"
type: module-action
module: queue
action: nack
fqn: queue.nack
short_name: QueueNack
keywords: [queue, nack, queuenack, write, rejeter, reject]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# queue.nack (QueueNack)

## Description
Reject messages - requeue for retry or send to dead-letter queue.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | - | Queue name. |
| `message_ids` | array | ✓ | - | List of ack_id values to reject. |
| `requeue` | boolean |  | `True` | True = retry later, False = send to dead-letter queue. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [nack]
```

## Aliases
`rejeter`, `reject`

## Safety
- Risk level: **low**
