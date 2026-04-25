---

id: triggers-recording
title: Triggers & Recording
sidebar_position: 8
format: md
---



# Triggers & Recording

LLMOS Bridge provides two complementary automation systems: **Triggers** for reactive event-driven plan execution, and **Recording** for capturing and replaying workflow sequences.

---


## Trigger System

### Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │               TriggerDaemon                  │
                    └─────────────────────────────────────────────┘
                                        |
              ┌─────────────────────────┼─────────────────────────┐
              |                         |                         |
    ┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────┐
    │  TriggerStore    │    │  PriorityFireSched.   │    │  ConflictResolver│
    │  (SQLite)        │    │  (heap queue, rate    │    │  (resource locks,│
    │                  │    │   limiting, preemption)│    │   wait/acquire)  │
    └─────────────────┘    └──────────────────────┘    └─────────────────┘
              |                         |
              v                         v
    ┌──────────────────────────────────────────────────────────────┐
    │                       Watcher Layer                          │
    │                                                              │
    │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
    │  │Cron      │  │Interval  │  │FileSystem│  │Process   │   │
    │  │Watcher   │  │Watcher   │  │Watcher   │  │Watcher   │   │
    │  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
    │  ┌──────────┐  ┌──────────┐  ┌──────────┐                  │
    │  │Resource  │  │Once      │  │Composite │                  │
    │  │Watcher   │  │Watcher   │  │Watcher   │                  │
    │  └──────────┘  └──────────┘  └──────────┘                  │
    └──────────────────────────────────────────────────────────────┘
```

---


### Trigger Lifecycle

```
register_trigger()
    |
    v
REGISTERED ──activate()──→ ACTIVE
    |                        |
    |                   Watcher._run()
    |                        |
    |                        v
    |                    WATCHING (composite only, partial match)
    |                        |
    |                   condition met
    |                        |
    |                        v
    |                     FIRED ──→ submit_plan()
    |                        |
    |               throttled?──→ THROTTLED (wait min_interval)
    |                        |
    |               error?───→ FAILED
    |                        |
    |                        v
    |                    ACTIVE (re-arm)
    |
    +──deactivate()──→ INACTIVE
    |
    +──delete()──→ (removed)
```

---


### TriggerDefinition

The complete persistent record for a trigger.

#### Identity

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `trigger_id` | str | UUID | Unique identifier |
| `name` | str | `""` | Human label |
| `description` | str | `""` | Description |

#### Condition

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `condition` | TriggerCondition | required | What to watch for |

#### Action

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `plan_template` | dict | `{}` | IML plan JSON fired when triggered |
| `plan_id_prefix` | str | `"trigger"` | Prefix for generated plan IDs |

#### Lifecycle

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `state` | TriggerState | REGISTERED | Current state |
| `priority` | TriggerPriority | NORMAL | Execution priority |
| `enabled` | bool | True | Whether trigger is active |

#### Throttling

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `min_interval_seconds` | float | 0.0 | Minimum seconds between fires |
| `max_fires_per_hour` | int | 0 | Rate limit (0 = unlimited) |

#### Conflict Resolution

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `conflict_policy` | str | `"queue"` | `queue`, `preempt`, or `reject` |
| `resource_lock` | str | None | Named resource for mutual exclusion |

#### Trigger Chaining

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `parent_trigger_id` | str | None | Parent trigger (chain source) |
| `chain_depth` | int | 0 | Current chain depth |
| `max_chain_depth` | int | 5 | Maximum chain depth |

#### Ownership

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `created_by` | str | `"user"` | `user`, `llm`, or `system` |
| `tags` | list[str] | `[]` | Classification tags |
| `expires_at` | float | None | Auto-delete timestamp (TTL) |

#### Health Metrics

| Field | Type | Description |
|-------|------|-------------|
| `fire_count` | int | Total fires |
| `fail_count` | int | Total failures |
| `throttle_count` | int | Times throttled |
| `last_fired_at` | float | Last fire timestamp |
| `last_error` | str | Last error message |
| `avg_latency_ms` | float | Exponential moving average |

---


### Trigger Types

#### TriggerType

| Type | Description |
|------|-------------|
| `TEMPORAL` | Time-based (cron, interval, one-shot) |
| `FILESYSTEM` | File/directory change events |
| `PROCESS` | Process start/stop/crash |
| `RESOURCE` | CPU/memory/disk thresholds |
| `APPLICATION` | Application events |
| `IOT` | GPIO pin state, MQTT messages |
| `COMPOSITE` | AND/OR/NOT/SEQ/WINDOW of other triggers |

### TriggerCondition Parameters

#### Temporal

| Parameter | Type | Description |
|-----------|------|-------------|
| `schedule` | str | Cron expression (e.g. `0 9 * * 1-5`) |
| `interval_seconds` | float | Repeat every N seconds |
| `run_at` | float | Unix timestamp for one-shot |

#### Filesystem

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | str | File or directory to watch |
| `recursive` | bool | Watch subdirectories |
| `events` | list[str] | `created`, `modified`, `deleted` |

#### Process

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | str | Process name pattern (fnmatch) |
| `event` | str | `started`, `stopped`, or `crashed` |

#### Resource

| Parameter | Type | Description |
|-----------|------|-------------|
| `metric` | str | `cpu_percent`, `memory_percent`, or `disk_percent` |
| `threshold` | float | Trigger when exceeded |
| `duration_seconds` | float | Must exceed for this duration |

#### IoT

| Parameter | Type | Description |
|-----------|------|-------------|
| `pin` | int | GPIO pin number |
| `edge` | str | `rising`, `falling`, or `both` |
| `mqtt_topic` | str | MQTT subscription topic |
| `mqtt_threshold` | float | MQTT value threshold |

#### Composite

| Parameter | Type | Description |
|-----------|------|-------------|
| `operator` | str | `AND`, `OR`, `NOT`, `SEQ`, `WINDOW` |
| `trigger_ids` | list[str] | Sub-trigger IDs |
| `timeout_seconds` | float | For SEQ/WINDOW operators |
| `count` | int | For WINDOW operator |
| `window_seconds` | float | Time window for WINDOW operator |

---


### Watchers

All watchers extend `BaseWatcher` (ABC):

| Method | Description |
|--------|-------------|
| `async start()` | Start background watching task |
| `async stop()` | Signal stop and wait for cleanup |
| `is_running` (property) | Whether watcher task is active |
| `async _run()` (abstract) | Main watch loop (subclass implements) |
| `async _fire(event_type, payload)` | Invoke fire callback |

#### WatcherFactory

Creates the correct watcher subclass from a `TriggerCondition`:

| Condition Type | Watcher Created |
|----------------|-----------------|
| TEMPORAL + cron | CronWatcher (requires `croniter`) |
| TEMPORAL + interval | IntervalWatcher |
| TEMPORAL + run_at | OnceWatcher |
| FILESYSTEM | FileSystemWatcher (requires `watchfiles`) |
| PROCESS | ProcessWatcher (requires `psutil`) |
| RESOURCE | ResourceWatcher (requires `psutil`) |
| COMPOSITE | CompositeWatcher |

---


### Priority Fire Scheduler

Orders triggered plan submissions by priority with rate limiting and preemption.

| Method | Description |
|--------|-------------|
| `async start()` | Start scheduling loop |
| `async stop()` | Stop loop |
| `async enqueue(trigger, fire_event)` | Add to priority queue |
| `queue_depth` (property) | Items waiting |
| `running_count` (property) | Currently executing |
| `on_plan_completed(plan_id)` | Called when plan finishes |

**Priority ordering** (highest first):
```
CRITICAL (4) > HIGH (3) > NORMAL (2) > LOW (1) > BACKGROUND (0)
```

**Rate limiting**: Sliding window per trigger (`max_fires_per_hour`). Excess fires are throttled.

**Preemption**: When `conflict_policy=preempt`, a higher-priority trigger can cancel a lower-priority running plan for the same resource.

---


### Conflict Resolver

In-memory resource lock table for mutual exclusion between triggers.

| Method | Description |
|--------|-------------|
| `async try_acquire(resource, plan_id, policy)` | Attempt lock acquisition |
| `async wait_for_resource(resource, timeout)` | Block until free |
| `release(resource, plan_id)` | Release lock |
| `is_locked(resource)` | Check lock status |
| `holder_of(resource)` | Get lock holder |

**Conflict policies**:

| Policy | Behavior |
|--------|----------|
| `queue` | Wait for resource to become free |
| `preempt` | Cancel current holder, take lock |
| `reject` | Fail immediately if locked |

---


### Trigger Store

SQLite persistence for trigger definitions.

| Method | Description |
|--------|-------------|
| `async init()` | Create tables and indices |
| `async save(trigger)` | Insert or update (upsert) |
| `async get(trigger_id)` | Load by ID |
| `async list_all()` | All triggers |
| `async load_active()` | Enabled + ACTIVE/WATCHING (for startup) |
| `async list_by_state(state)` | Filter by state |
| `async update_state(trigger_id, state)` | Fast state update |
| `async delete(trigger_id)` | Delete trigger |
| `async purge_expired()` | Delete past `expires_at` |

---


### TriggerDaemon

Main orchestrator for the trigger subsystem.

| Method | Description |
|--------|-------------|
| `async start()` | Init store, load active triggers, start watchers |
| `async stop()` | Stop all watchers and scheduler |
| `async register(trigger)` | Register and optionally activate |
| `async activate(trigger_id)` | Enable and arm watcher |
| `async deactivate(trigger_id)` | Disarm without deleting |
| `async delete(trigger_id)` | Permanently remove |
| `async get(trigger_id)` | Get trigger definition |
| `async list_all()` | All triggers |
| `async list_active()` | ACTIVE/WATCHING triggers |

**Plan submission flow**:
```
Watcher fires → _on_watcher_fire()
    |
    +--→ Check min_interval (throttle if too soon)
    |
    +--→ Check conflict_policy (lock resource)
    |
    +--→ Scheduler.enqueue(trigger, fire_event)
    |
    +--→ Scheduler dequeues by priority
    |
    +--→ _submit_plan(trigger, fire_event)
    |       |
    |       +--→ _build_plan() with template variables
    |       +--→ executor.execute_plan(plan)
    |
    +--→ EventBus.emit("llmos.triggers", event)
```

**Health loop**: Background task that periodically checks watcher health and purges expired triggers.

---


## Recording System

### Architecture

```
    ┌─────────────────────────────────────────────┐
    │           WorkflowRecorder                   │
    │  (manages active recording session)          │
    └─────────────────────────────────────────────┘
                         |
              ┌──────────┼──────────┐
              |                     |
    ┌─────────────────┐   ┌─────────────────┐
    │  RecordingStore  │   │  WorkflowReplayer│
    │  (SQLite)        │   │  (plan generator)│
    └─────────────────┘   └─────────────────┘
```

---


### WorkflowRecording

| Field | Type | Description |
|-------|------|-------------|
| `recording_id` | str | Unique identifier |
| `title` | str | Recording title |
| `description` | str | Description |
| `status` | RecordingStatus | `ACTIVE` or `STOPPED` |
| `created_at` | float | Start timestamp |
| `stopped_at` | float | Stop timestamp (if stopped) |
| `plans` | list[RecordedPlan] | Captured plan executions |
| `generated_plan` | dict | IML replay plan (generated on stop) |

### RecordedPlan

| Field | Type | Description |
|-------|------|-------------|
| `plan_id` | str | Original plan ID |
| `sequence` | int | 1-based position in recording |
| `added_at` | float | Capture timestamp |
| `plan_data` | dict | Original IML plan JSON |
| `final_status` | str | Plan completion status |
| `action_count` | int | Number of actions in plan |

---


### WorkflowRecorder

Single-session manager. Only one recording can be active at a time.

| Method | Description |
|--------|-------------|
| `async start(title, description)` | Start new recording session |
| `async stop(recording_id)` | Stop and generate replay plan |
| `async add_plan(recording_id, plan_data, final_status, action_count)` | Append completed plan |

**Auto-stop**: Starting a new recording automatically stops the previous one.

**Auto-tagging**: The executor calls `add_plan()` after each plan completes. Plans submitted during an active recording are captured silently.

---


### WorkflowReplayer

Generates a single IML plan that replays an entire recording.

| Method | Description |
|--------|-------------|
| `generate(recording)` | Merge recorded plans into sequential IML plan |
| `generate_llm_context(recording)` | Human-readable summary for LLM |

**Replay plan generation**:
```
Recording with 3 plans (A, B, C), each with actions:
  Plan A: a1, a2, a3
  Plan B: b1, b2
  Plan C: c1

Generated replay plan:
  Actions: p1_a1, p1_a2, p1_a3, p2_b1, p2_b2, p3_c1
  Dependencies:
    p2_b1 depends_on p1_a3  (last action of plan 1)
    p3_c1 depends_on p2_b2  (last action of plan 2)
```

Action IDs are prefixed (`p1_`, `p2_`, etc.) to avoid collisions. Plans are chained sequentially.

---


### Recording Store

SQLite persistence with two tables: `recordings` (header) and `recorded_plans` (captured plans).

| Method | Description |
|--------|-------------|
| `async init()` | Create tables |
| `async save(recording)` | Insert or replace header |
| `async add_plan(recording_id, plan)` | Append recorded plan |
| `async update_status(recording_id, status, stopped_at, generated_plan)` | Update recording status |
| `async delete(recording_id)` | Delete recording + plans |
| `async get(recording_id)` | Load complete recording |
| `async list_all(status)` | List recordings (optional filter) |

---


### Configuration

```yaml
recording:
  enabled: false           # Enable recording system
  db_path: ~/.llmos/recordings.db

triggers:
  enabled: false           # Enable trigger system
  db_path: ~/.llmos/triggers.db
  max_concurrent_plans: 5  # Max triggered plans running simultaneously
  max_chain_depth: 5       # Max trigger chain depth
  enabled_types:           # Enabled trigger types
    - temporal
    - filesystem
    - process
    - resource
    - composite
```
