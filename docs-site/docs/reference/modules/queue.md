---
id: queue
title: queue Module
sidebar_label: queue
sidebar_position: 13
description: Async message queue - InMemory + Redis Streams, consumer groups, dead-letter, priorities, delays.
---

# queue

Event-driven async message queue. Priority ordering, consumer
groups, dead-letter, delayed delivery, background subscriptions
that notify the agent when messages arrive.

| Property | Value |
|----------|-------|
| Module id | `queue` |
| Version | `1.0.0` |
| Type | user |
| Pip deps | `redis` (only for the Redis backend) |

## Backends

| Backend | URL scheme | Storage | Consumer groups |
|---------|------------|---------|-----------------|
| `InMemoryQueueBackend` | `null` (default) | per-queue `heapq` | simulated |
| `RedisQueueBackend` | `redis://host:6379/0` | Redis Streams | native (XADD / XREADGROUP / XACK) |

Dead-letter queue naming: `dlq:{app_id}:{queue_name}`.

## The 13 actions



| Tool | Source | Purpose |
|------|--------|---------|
| `queue.create_queue` | | Create / ensure a queue. Sets `max_size`, `dead_letter_enabled`, `max_retries`. |
| `queue.publish` | | Publish a message. Optional `priority` (0=highest, 9=lowest), `delay_seconds`, `consumer_group`, `headers`. |
| `queue.subscribe` | | Start a background consumer that pushes arriving messages via stream notifications. |
| `queue.unsubscribe` | | Stop a background subscription. |
| `queue.receive` | | Pull mode - blocking with timeout. `ack_mode='manual'` to ack after processing. |
| `queue.ack` | | Acknowledge messages after success. |
| `queue.nack` | | Reject - `requeue` to retry, otherwise → dead-letter. |
| `queue.peek` | | Preview without consuming. |
| `queue.queue_stats` | | Depth, consumer count, throughput. |
| `queue.list_queues` | | All known queues. |
| `queue.delete_queue` | | Delete a queue + all messages. |
| `queue.purge` | | Remove all messages, keep the queue. |
| `queue.dead_letter` | | Inspect the DLQ for messages that exceeded `max_retries`. |

## Message format

```python
QueueMessage(
    id: str,                    # uuid4
    queue: str,
    body: Any,                  # JSON-serialisable
    headers: dict[str, str],
    priority: int,              # 0..9, 0=highest
    timestamp: float,
    attempts: int,
    max_retries: int,
    delay_until: float | None,  # epoch timestamp for delayed delivery
    consumer_group: str | None,
    ack_id: str | None,
)
```

## Configuration

```yaml
tools:
  modules:
    queue:
      config:
        backend_url: null                # null = InMemory; redis://host:6379/0
        default_max_retries: 3
        default_visibility_timeout: 30   # seconds before a non-acked message reappears
        max_queues: 50
      constraints:
        allowed_queues: [orders, events, notifications]
        max_queues: 100
```

## Constraints

:

| Constraint | Type | Default | Description |
|------------|------|---------|-------------|
| `allowed_queues` | `string_list` | unrestricted | Restrict which queue names the agent can use. |
| `max_queues` | `integer` | `50` | Maximum queues this app may create. |

## Aliases (FR / EN)

A few highlights - full list in `@action(aliases=[...])`:

| Action | Aliases |
|--------|---------|
| `queue.publish` | `publier`, `envoyer`, `send`, `emit` |
| `queue.subscribe` | `abonner`, `ecouter`, `listen` |
| `queue.receive` | `recevoir`, `pull`, `poll` |
| `queue.ack` | `confirmer`, `acknowledge` |
| `queue.nack` | `rejeter`, `reject` |
| `queue.dead_letter` | `lettres_mortes`, `dlq` |

## Cross-references

- App-config block reference (`tools.modules.queue`):
  [App Configuration → tools.modules](../../language/02-app-config.md#toolsmodules---module-configuration)
- Channels module bridges to queues via the `queue` adapter:
  [Channels → queue adapter](../../language/40-channels.md#queue---inter-app-bus-bidirectional)
- Background sessions for multi-user routing of queue events:
  [Background Sessions](../../language/38-background-sessions.md)
