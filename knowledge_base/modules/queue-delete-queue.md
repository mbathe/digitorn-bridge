---
id: queue-delete-queue
title: "queue.delete_queue (QueueDeleteQueue)"
type: module-action
module: queue
action: delete_queue
fqn: queue.delete_queue
short_name: QueueDeleteQueue
keywords: [queue, delete_queue, queuedeletequeue, admin, supprimer_file]
permissions: []
risk_level: high
irreversible: true
require_approval: false
---

# queue.delete_queue (QueueDeleteQueue)

## Description
Delete a queue and all its messages permanently.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | — | Queue to delete. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [delete_queue]
```

## Aliases
`supprimer_file`

## Safety
- Risk level: **high**
- ⚠️ **Irreversible** — cannot be undone once executed
