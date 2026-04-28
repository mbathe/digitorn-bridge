---
id: queue-subscribe
title: "queue.subscribe (QueueSubscribe)"
type: module-action
module: queue
action: subscribe
fqn: queue.subscribe
short_name: QueueSubscribe
keywords: [queue, subscribe, queuesubscribe, read, abonner, ecouter, listen]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# queue.subscribe (QueueSubscribe)

## Description
Start consuming messages from a queue in the background. You will be notified when messages arrive.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | - | Queue to subscribe to. |
| `batch_size` | integer |  | `1` | Messages per batch. |
| `filter_headers` | object |  | - | Only receive messages whose headers match these key-value pairs. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [subscribe]
```

## Aliases
`abonner`, `ecouter`, `listen`

## Safety
- Risk level: **low**
