---
id: queue-publish
title: "queue.publish (QueuePublish)"
type: module-action
module: queue
action: publish
fqn: queue.publish
short_name: QueuePublish
keywords: [queue, publish, queuepublish, write, publier, envoyer, send, emit]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# queue.publish (QueuePublish)

## Description
Publish a message to a queue with optional priority and delay.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `queue` | string | ✓ | — | Target queue name. |
| `message` | string | ✓ | — | Message body (any JSON-serializable value). |
| `priority` | integer |  | `5` | Priority 0 (highest) to 9 (lowest). |
| `delay_seconds` | number |  | `0` | Hold message for N seconds before delivery. |
| `headers` | object |  | — | Message headers/metadata. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [publish]
```

## Aliases
`publier`, `envoyer`, `send`, `emit`

## Safety
- Risk level: **medium**
