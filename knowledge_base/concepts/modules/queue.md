---
id: module-concept-queue
title: "queue module - overview"
type: module-concept
module: queue
isolation: shared
keywords: [queue, queue-module, create_queue, publish, subscribe, unsubscribe, receive, ack, nack, peek, queue_stats, list_queues, delete_queue, purge, dead_letter]
version: 1.0.0
---

# `queue` module

- **Isolation**: `shared` (one instance shared across apps)
- **Version**: `1.0.0`
- **Actions**: 13 visible, 0 internal

## Description (from class docstring)

Queue module - event-driven message queue with InMemory and Redis backends.

Agents publish and consume messages with priorities, dead-letter queues,
delayed delivery, and consumer group semantics.

## Configuration

Set under `modules.queue.config` in `app.yaml`. All fields derive from the module's Pydantic config model.

| Field | Type | Required | Default | Description |
|-------|------|:--------:|---------|-------------|
| `workspace` | str |  | `''` | Auto-injected by the daemon at module init time. Do NOT set manually in YAML - the daemon resolves it from the app's workspace/workspace_mode config. |
| `backend_url` | str \| None |  | `None` | Queue backend URL (None=in-memory, redis://...=Redis Streams) |
| `default_max_retries` | int |  | `3` | Default max retries before dead-letter |

## Actions

| Action | Short name | Internal | Risk | One-liner |
|--------|-----------|:--------:|------|-----------|
| `create_queue` | `QueueCreateQueue` |  | medium | Create or ensure a named message queue exists. |
| `publish` | `QueuePublish` |  | medium | Publish a message to a queue with optional priority and delay. |
| `subscribe` | `QueueSubscribe` |  | low | Start consuming messages from a queue in the background. You will be notified when messages arrive. |
| `unsubscribe` | `QueueUnsubscribe` |  | low | Stop a background subscription. |
| `receive` | `QueueReceive` |  | low | Pull messages from a queue (poll mode). Use ack_mode='manual' to acknowledge after processing. |
| `ack` | `QueueAck` |  | low | Acknowledge messages after successful processing. |
| `nack` | `QueueNack` |  | low | Reject messages - requeue for retry or send to dead-letter queue. |
| `peek` | `QueuePeek` |  | low | Preview messages without consuming them. |
| `queue_stats` | `QueueQueueStats` |  | low | Get queue statistics: depth, consumer count, throughput. |
| `list_queues` | `QueueListQueues` |  | low | List all known queues. |
| `delete_queue` | `QueueDeleteQueue` |  | high | Delete a queue and all its messages permanently. |
| `purge` | `QueuePurge` |  | high | Remove all messages from a queue without deleting the queue. |
| `dead_letter` | `QueueDeadLetter` |  | low | View messages in the dead-letter queue - messages that failed after max retries. |

## Grant (in `capabilities.grant`)

Full-app grant (every visible action):

```yaml
capabilities:
  grant:
    - module: queue
      actions: [create_queue, publish, subscribe, unsubscribe, receive, ack, nack, peek, queue_stats, list_queues, delete_queue, purge, dead_letter]
```

Per-specialist grant (under `agents[].modules`):

```yaml
agents:
  - id: my-agent
    modules:
      - {queue: [create_queue, publish, subscribe, unsubscribe, receive]}
```

## Per-action cards

For the full parameter spec of each action, see the auto-generated cards in `knowledge_base/modules/queue-*.md`.
