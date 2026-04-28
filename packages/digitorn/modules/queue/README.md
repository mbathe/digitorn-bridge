# Queue Module

Event-driven message queue with InMemory and Redis Streams backends.

## Overview

The Queue module provides pub/sub messaging, consumer groups, dead-letter
queues, message priorities, and delayed delivery. Agents publish events,
subscribe to queues, and coordinate work across multiple consumers.

Two backends:

| Backend | Storage | Pattern | Multi-worker |
|---------|---------|---------|-------------|
| `InMemoryQueueBackend` | `heapq` priority queues | Dev/test | No |
| `RedisQueueBackend` | Redis Streams (XADD/XREADGROUP/XACK) | Production | Yes |

Factory: `create_queue_backend(url, app_id)` - `None`=InMemory, `redis://`=Redis.

## Key Features

- **Priority messages** - 0 (highest) to 9 (lowest), sorted in receive order
- **Consumer groups** - Redis Streams native XREADGROUP for parallel consumers
- **Dead-letter queue** - failed messages after max retries go to DLQ
- **Delayed delivery** - publish with `delay_until` to hold messages
- **Subscriptions** - `subscribe()` creates asyncio background tasks with stream notifications
- **Auto/manual ack** - choose between automatic and explicit acknowledgment

## Actions (13)

| Action | Description | Risk |
|--------|-------------|------|
| **Queue management** | | |
| `create_queue` | Create a named queue | Medium |
| `delete_queue` | Delete a queue and all messages | High |
| `list_queues` | List all queues with stats | Low |
| `purge` | Remove all messages from a queue | High |
| **Messaging** | | |
| `publish` | Publish a message with priority and headers | Medium |
| `receive` | Receive messages (blocking with timeout) | Low |
| `ack` | Acknowledge successful processing | Low |
| `nack` | Reject a message (re-queue or dead-letter) | Low |
| `peek` | Preview messages without consuming | Low |
| **Subscriptions** | | |
| `subscribe` | Start background consumer with notifications | Low |
| `unsubscribe` | Stop a subscription | Low |
| **Dead-letter** | | |
| `dead_letter` | View dead-letter queue messages | Low |
| **Stats** | | |
| `queue_stats` | Detailed queue statistics | Low |

## Architecture

```
QueueModule
    │
    ├── QueueBackend (protocol, async)
    │       │
    │       ├── InMemoryQueueBackend
    │       │       ├── heapq per queue (priority ordering)
    │       │       ├── asyncio.Event for blocking receive
    │       │       └── delayed message promotion task
    │       │
    │       └── RedisQueueBackend
    │               ├── Redis Streams (XADD/XREADGROUP/XACK)
    │               ├── Consumer groups (native)
    │               └── DLQ: separate stream dlq:{app_id}:{queue}
    │
    ├── QueueMessage (dataclass)
    │       id, queue, body, headers, priority,
    │       timestamp, attempts, max_retries,
    │       delay_until, consumer_group, ack_id
    │
    └── Subscriptions
            dict[queue_name, asyncio.Task]
            → poll via receive() → notify via _notify_bg()
```

## App YAML Configuration

```yaml
modules:
  queue:
    config:
      backend_url: null              # null=InMemory, redis://host:6379/0
      default_max_retries: 3
      default_visibility_timeout: 30
      max_queues: 50
```

## LLM Usage

```
1. queue.create_queue  →  create "tasks" queue
2. queue.publish       →  publish work item with priority
3. queue.subscribe     →  start background consumer
4. queue.dead_letter   →  inspect failed messages
5. queue.queue_stats   →  monitor queue depth and throughput
```
