---

id: events
title: Event System
sidebar_position: 3
format: md
---



# Event System

The EventBus is the communication backbone of LLMOS Bridge. Every significant operation — plan execution, action dispatch, security checks, permission changes, perception captures — emits events through the bus. Consumers subscribe to topics and react independently.

---


## Architecture

```
Producers                          EventBus                         Consumers
──────────                         ────────                         ─────────
PlanExecutor  ─┐                                                ┌─ SSE endpoint
AuditLogger   ─┤  emit(topic, event)  ┌──────────────┐         ├─ WebSocket
PermissionMgr ─┤ ─────────────────────→│  _stamp()    │─────────├─ AuditLogger (file)
ModuleManager ─┤                      │  ring buffer │         ├─ TriggerDaemon
ActionStream  ─┘                      │  dispatch    │─────────├─ Dashboard
                                      └──────────────┘         └─ Custom listeners
```

---


## Topics

12 standard topics route events to the correct consumers:

| Topic | Constant | Purpose |
|-------|----------|---------|
| `llmos.plans` | `TOPIC_PLANS` | Plan lifecycle (submitted, running, completed, failed, cancelled) |
| `llmos.actions` | `TOPIC_ACTIONS` | Action execution (started, completed, failed, skipped) |
| `llmos.actions.progress` | `TOPIC_ACTION_PROGRESS` | Streaming progress from `@streams_progress` actions |
| `llmos.actions.results` | `TOPIC_ACTION_RESULTS` | Final action results (completion notifications) |
| `llmos.security` | `TOPIC_SECURITY` | Permission denials, sensitive actions, violations |
| `llmos.errors` | `TOPIC_ERRORS` | Unhandled runtime errors |
| `llmos.perception` | `TOPIC_PERCEPTION` | Screenshot/OCR capture events |
| `llmos.permissions` | `TOPIC_PERMISSIONS` | Permission grant/revoke events |
| `llmos.modules` | `TOPIC_MODULES` | Module load/unload/state changes |
| `llmos.iot` | `TOPIC_IOT` | IoT sensor readings (Phase 4) |
| `llmos.db.changes` | `TOPIC_DB` | Database change data capture (Phase 4) |
| `llmos.filesystem` | `TOPIC_FILESYSTEM` | Filesystem change events (Phase 4) |

---


## EventBus ABC

All event bus implementations share this interface:

| Method | Description |
|--------|-------------|
| `async emit(topic, event)` | Publish event to topic (abstract) |
| `async subscribe(topics)` | Async iterator of events (optional) |
| `register_listener(topic, callback)` | Register async callback |
| `unregister_listener(topic, callback)` | Remove callback |
| `unregister_all_listeners(callback)` | Remove from all topics |

**Event stamping**: Every event is enriched with `_topic` and `_timestamp` fields via `_stamp()`, and appended to a ring buffer (last 500 events).

**Listener dispatch**: `_dispatch_to_listeners()` invokes all registered callbacks. Exceptions in listeners are caught and logged — they never propagate to the emitter.

---


## Implementations

### NullEventBus (default)

Zero-overhead no-op. Events are discarded but listeners still receive callbacks.

Use when: No event persistence or streaming is needed.

### LogEventBus

Appends events as NDJSON (newline-delimited JSON) to a file. Thread-safe via `asyncio.Lock`.

| Method | Description |
|--------|-------------|
| `emit(topic, event)` | Async append to log file |
| `emit_sync(topic, event)` | Synchronous variant for module `__init__()` |

Use when: Audit trail persistence to disk is required.

### FanoutEventBus

Broadcasts events to multiple backends in parallel via `asyncio.gather()`.

```python
bus = FanoutEventBus(backends=[
    LogEventBus(Path("/var/log/llmos/events.ndjson")),
    websocket_bus,
    redis_bus,  # Phase 4
])
```

Use when: Events must be delivered to multiple destinations simultaneously.

---


## EventRouter

Extends EventBus with MQTT-style wildcard pattern matching:

```python
router = EventRouter(base_bus=log_bus)
router.add_route("llmos.actions.*", handle_all_actions)
router.add_route("llmos.security.#", handle_security)
```

| Pattern | Matches |
|---------|---------|
| `llmos.actions.*` | Single-level wildcard (e.g., `llmos.actions.progress`) |
| `llmos.security.#` | Multi-level wildcard (e.g., `llmos.security.audit.detailed`) |
| `llmos.plans` | Exact match |

---


## UniversalEvent

Causality-aware event envelope for structured event tracking:

| Field | Type | Description |
|-------|------|-------------|
| `event_id` | string | Unique event identifier |
| `topic` | string | Event topic |
| `event_type` | string | Event type name |
| `timestamp` | float | Unix timestamp |
| `source` | string | Emitter identifier |
| `data` | dict | Event payload |
| `causation_id` | string | ID of the event that caused this one |
| `correlation_id` | string | Shared ID across related events |
| `priority` | EventPriority | CRITICAL, HIGH, NORMAL, LOW, BACKGROUND |

Methods: `to_dict()`, `from_dict()`, `spawn_child()`.

The `spawn_child()` method creates a new event that inherits the correlation chain, enabling distributed tracing.

---


## EventPriority

| Priority | Value | Description |
|----------|-------|-------------|
| `CRITICAL` | 0 | Security violations, system errors |
| `HIGH` | 1 | Plan failures, permission denials |
| `NORMAL` | 2 | Standard operations |
| `LOW` | 3 | Informational events |
| `BACKGROUND` | 4 | Metrics, diagnostics |

---


## SessionContextPropagator

Maps plan execution to trigger context, enabling session-aware event routing:

| Method | Description |
|--------|-------------|
| `bind(plan_id, context)` | Associate context with a plan |
| `get(plan_id)` | Retrieve context |
| `unbind(plan_id)` | Remove association |
| `active_count` | Number of active sessions |
| `active_plan_ids()` | List of tracked plan IDs |

---


## Backend Swap Strategy

The EventBus is designed for zero-change backend swapping:

```
Phase 1-2: NullEventBus or LogEventBus
Phase 3:   FanoutEventBus(Log + WebSocket)
Phase 4:   FanoutEventBus(Log + WebSocket + RedisStreamsBus)
Phase 5:   FanoutEventBus(Log + WebSocket + KafkaBus)
```

Producer code (`PlanExecutor.emit()`, `AuditLogger.log()`) never changes. Only the bus constructor in `create_app()` changes.
