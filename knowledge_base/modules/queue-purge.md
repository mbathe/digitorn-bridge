---
id: queue-purge
title: "queue.purge (QueuePurge)"
type: module-action
module: queue
action: purge
fqn: queue.purge
short_name: QueuePurge
keywords: [queue, purge, queuepurge, admin, vider_file, clear_queue]
permissions: []
risk_level: high
irreversible: true
require_approval: false
---

# queue.purge (QueuePurge)

## Description
Remove all messages from a queue without deleting the queue.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | — | Queue to purge. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [purge]
```

## Aliases
`vider_file`, `clear_queue`

## Safety
- Risk level: **high**
- ⚠️ **Irreversible** — cannot be undone once executed
