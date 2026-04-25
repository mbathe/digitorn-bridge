---
id: queue-list-queues
title: "queue.list_queues (QueueListQueues)"
type: module-action
module: queue
action: list_queues
fqn: queue.list_queues
short_name: QueueListQueues
keywords: [queue, list_queues, queuelistqueues, info, lister_files]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# queue.list_queues (QueueListQueues)

## Description
List all known queues.

## Parameters
_(no parameters)_

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [list_queues]
```

## Aliases
`lister_files`

## Safety
- Risk level: **low**
