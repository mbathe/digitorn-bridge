---
id: queue-queue-stats
title: "queue.queue_stats (QueueQueueStats)"
type: module-action
module: queue
action: queue_stats
fqn: queue.queue_stats
short_name: QueueQueueStats
keywords: [queue, queue_stats, queuequeuestats, info, statistiques_file, info_file]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# queue.queue_stats (QueueQueueStats)

## Description
Get queue statistics: depth, consumer count, throughput.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | — | Queue name. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [queue_stats]
```

## Aliases
`statistiques_file`, `info_file`

## Safety
- Risk level: **low**
