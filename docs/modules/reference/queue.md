---
id: queue
title: Queue Module
sidebar_label: queue
sidebar_position: 13
description: Event-driven message queue — InMemory and Redis Streams backends, consumer groups, dead-letter, priorities.
---

# queue

Event-driven message queue with priority ordering, consumer groups, dead-letter queues, delayed delivery, and background subscriptions.

| Property | Value |
|----------|-------|
| **Module ID** | `queue` |
| **Version** | `1.0.0` |
| **Platforms** | All |
| **Dependencies** | `redis` (Redis backend) |

---

## Design Philosophy

- **Async-first** — all backend operations are async, using `asyncio.Event` for blocking receive
- **Redis Streams native** — XADD/XREADGROUP/XACK for production consumer groups
- **Priority ordering** — messages sorted by priority (0=highest, 9=lowest)
- **Dead-letter** — failed messages after max retries are moved to DLQ for inspection
- **Subscription model** — `subscribe()` creates background asyncio Tasks that notify the agent

---

## Actions (13)

### create_queue
Create a named message queue. Parameters: `name`, `max_size`, `dead_letter_enabled`, `max_retries`. **Risk: medium**

### publish
Publish a message with priority and headers. Parameters: `queue`, `body`, `headers`, `priority`, `delay_seconds`, `consumer_group`. **Risk: medium**

### subscribe
Start a background consumer that pushes messages via stream notifications. Parameters: `queue`, `consumer_group`, `batch_size`, `ack_mode`. **Risk: low**

### unsubscribe
Stop a background subscription. Parameters: `queue`. **Risk: low**

### receive
Receive messages (blocking with timeout). Parameters: `queue`, `consumer_group`, `count`, `timeout`, `ack_mode`. **Risk: low**

### ack
Acknowledge successful message processing. Parameters: `queue`, `ack_id`. **Risk: low**

### nack
Reject a message (re-queue or send to dead-letter). Parameters: `queue`, `ack_id`, `requeue`. **Risk: low**

### peek
Preview messages without consuming them. Parameters: `queue`, `count`. **Risk: low**

### queue_stats
Detailed queue statistics: depth, consumer count, throughput. Parameters: `queue`. **Risk: low**

### list_queues
List all queues with summary stats. **Risk: low**

### delete_queue
Delete a queue and all its messages. Parameters: `queue`. **Risk: high**

### purge
Remove all messages from a queue without deleting it. Parameters: `queue`. **Risk: high**

### dead_letter
View dead-letter queue messages. Parameters: `queue`, `limit`. **Risk: low**

---

## Message Format

```python
QueueMessage:
    id: str                    # uuid4
    queue: str
    body: Any                  # JSON-serializable
    headers: dict[str, str]
    priority: int              # 0=highest, 9=lowest
    timestamp: float
    attempts: int
    max_retries: int
    delay_until: float | None  # epoch timestamp for delayed delivery
    consumer_group: str | None
    ack_id: str | None
```

---

## Backends

| Backend | URL scheme | Storage | Consumer groups |
|---------|-----------|---------|----------------|
| `InMemoryQueueBackend` | `null` (default) | `heapq` per queue | Simulated |
| `RedisQueueBackend` | `redis://host:6379/0` | Redis Streams | Native (XREADGROUP) |

Dead-letter queue: `dlq:{app_id}:{queue_name}`

---

## Configuration

```yaml
modules:
  queue:
    config:
      backend_url: null              # null=InMemory, redis://host:6379/0
      default_max_retries: 3
      default_visibility_timeout: 30
      max_queues: 50
```
---

## Aliases (FR/EN)

| Action | Aliases |
|--------|---------|
| `create_queue` | `creer_file`, `nouvelle_file` |
| `publish` | `publier`, `envoyer`, `send`, `emit` |
| `subscribe` | `abonner`, `ecouter`, `listen` |
| `unsubscribe` | `desabonner` |
| `receive` | `recevoir`, `pull`, `poll` |
| `ack` | `confirmer`, `acknowledge` |
| `nack` | `rejeter`, `reject` |
| `peek` | `apercu`, `preview` |
| `queue_stats` | `statistiques_file`, `info_file` |
| `list_queues` | `lister_files` |
| `delete_queue` | `supprimer_file` |
| `purge` | `vider_file`, `clear_queue` |
| `dead_letter` | `lettres_mortes`, `dlq` |
