---
id: queue-unsubscribe
title: "queue.unsubscribe (QueueUnsubscribe)"
type: module-action
module: queue
action: unsubscribe
fqn: queue.unsubscribe
short_name: QueueUnsubscribe
keywords: [queue, unsubscribe, queueunsubscribe, admin, desabonner]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# queue.unsubscribe (QueueUnsubscribe)

## Description
Stop a background subscription.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `subscription_id` | string | ✓ | — | Subscription ID returned by subscribe. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [unsubscribe]
```

## Aliases
`desabonner`

## Safety
- Risk level: **low**
