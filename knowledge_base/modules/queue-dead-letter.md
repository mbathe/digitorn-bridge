---
id: queue-dead-letter
title: "queue.dead_letter (QueueDeadLetter)"
type: module-action
module: queue
action: dead_letter
fqn: queue.dead_letter
short_name: QueueDeadLetter
keywords: [queue, dead_letter, queuedeadletter, read, lettres_mortes, dlq]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# queue.dead_letter (QueueDeadLetter)

## Description
View messages in the dead-letter queue — messages that failed after max retries.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | — | Queue whose dead-letter messages to view. |
| `count` | integer |  | `10` | Number of dead-letter messages to return. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [dead_letter]
```

## Aliases
`lettres_mortes`, `dlq`

## Safety
- Risk level: **low**
