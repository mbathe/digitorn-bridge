---
id: queue-create-queue
title: "queue.create_queue (QueueCreateQueue)"
type: module-action
module: queue
action: create_queue
fqn: queue.create_queue
short_name: QueueCreateQueue
keywords: [queue, create_queue, queuecreatequeue, admin, creer_file, nouvelle_file]
permissions: []
risk_level: medium
irreversible: false
require_approval: false
---

# queue.create_queue (QueueCreateQueue)

## Description
Create or ensure a named message queue exists.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | string | ✓ | — | Queue name (alphanumeric + hyphens). |
| `config` | object |  | — | Backend-specific config (e.g. visibility_timeout, max_retries). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: queue
      actions: [create_queue]
```

## Aliases
`creer_file`, `nouvelle_file`

## Safety
- Risk level: **medium**
