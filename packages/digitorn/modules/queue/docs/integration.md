# queue — Integration Guide

`queue` gives agents a simple **named-queue primitive** — publish
messages, consume them in order, peek/clear queues. The backing store
is the daemon's KV backend (SQLite by default, Redis Streams when
`server.kv_backend` is set to a `redis://` URL).

## Typical actions

| Action | Purpose |
|---|---|
| `create_queue` | Create (or ensure) a named queue exists. |
| `publish` | Push a message onto a queue. |
| `consume` | Pop the next message (FIFO by default). |
| `peek` | Look at the head without consuming. |
| `size` | How many messages are queued. |
| `clear` | Drop all pending messages. |
| `list_queues` | Enumerate the queues visible to this app. |
| `delete_queue` | Remove a queue + all its messages. |

(See `docs/actions.md` for the full list and parameter shapes.)

## How queues fit into background apps

```
inbound channel (webhook, cron, file_watcher)
        │
        ▼
queue.publish(queue="emails", message={...})
        │
        ▼
background worker consumes N messages per tick
        │
        ▼
queue.consume → agent tool_call → downstream side-effect
```

This pattern decouples "something arrived" from "something was
processed" and gives the agent loop a natural checkpoint boundary.

## Constraints

| Constraint | Type | Scope | Default | Purpose |
|---|---|---|---|---|
| `allowed_queues` | `string_list` | module | — | Restrict which queue names the agent can reach (whitelist). |
| `max_queues` | `integer` | module | 50 | Cap the number of distinct queue names this app can create. |

## Isolation

`queue` is `shared` per daemon — queue names are scoped by
`(app_id, queue_name)` so two apps can both have a queue called
`inbox` without collision.

## Persistence

Messages survive daemon restart when the KV backend is persistent
(SQLite / Redis). Consuming a message is a transactional dequeue: if
the consumer crashes before acknowledging, the backend can retry based
on `visibility_timeout` (Redis Streams) or lease semantics (SQLite).

## When NOT to use

- In-memory throw-away buffers inside a single turn → just use a list.
- Heavy streaming (thousands of msg/s) — this module is tuned for
  agent workflows (dozens per minute), not a production message bus.
  Point at a dedicated broker in that case.

## Related

- `docs/configuration.md#kv_backend` — switching SQLite ↔ Redis
- `modules/channels` — queue is one of the 11 channel adapter types,
  so you can also consume a queue as an inbound trigger for an app.
