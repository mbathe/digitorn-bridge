---
id: queue-peek
title: "queue.peek (QueuePeek)"
type: module-action
module: queue
action: peek
fqn: queue.peek
short_name: QueuePeek
keywords: [queue, peek, queuepeek, read, apercu, preview]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# queue.peek (QueuePeek)

## Description
Preview messages without consuming them.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | — | Queue to peek into. |
| `count` | integer |  | `5` | Number of messages to preview. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [peek]
```

## Aliases
`apercu`, `preview`

## Safety
- Risk level: **low**
