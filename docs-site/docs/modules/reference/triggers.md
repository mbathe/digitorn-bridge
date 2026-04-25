---

id: triggers
title: Triggers Module
sidebar_position: 6
format: md
---




# triggers

Reactive automation through condition-based triggers. Register triggers that fire IML plans when conditions are met: time schedules (cron), filesystem events, process events, resource thresholds, or webhook callbacks.

| Property | Value |
|----------|-------|
| **Module ID** | `triggers` |
| **Version** | `1.0.0` |
| **Type** | system |
| **Platforms** | All |
| **Dependencies** | None |
| **Configuration** | `trigger.enabled = true` (disabled by default) |

---


## Actions (6)

### register_trigger

Register a new trigger with condition and plan template.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | Yes | — | Trigger name |
| `trigger_type` | string | Yes | — | `schedule`, `event`, `webhook` |
| `condition` | object | Yes | — | Trigger-specific condition |
| `plan_template` | object | Yes | — | IML plan to fire when triggered |
| `priority` | string | No | `"normal"` | `background`, `low`, `normal`, `high`, `critical` |
| `conflict_policy` | string | No | `"queue"` | `queue`, `skip`, `replace`, `merge` |
| `min_interval_seconds` | integer | No | `0` | Minimum interval between firings |
| `max_fires_per_hour` | integer | No | `null` | Rate limit |
| `ttl_seconds` | integer | No | `null` | Auto-expiry |
| `resource_locks` | array | No | `[]` | Mutual exclusion locks |

**Security**:
- `@requires_permission(Permission.PROCESS_EXECUTE)`
- `@audit_trail("standard")`

### Condition Examples

**Schedule (cron)**:
```json
{
  "trigger_type": "schedule",
  "condition": {
    "cron": "0 9 * * 1-5",
    "timezone": "America/New_York"
  }
}
```

**Event (filesystem)**:
```json
{
  "trigger_type": "event",
  "condition": {
    "event_type": "filesystem",
    "path": "/home/user/incoming",
    "events": ["created", "modified"]
  }
}
```

**Webhook**:
```json
{
  "trigger_type": "webhook",
  "condition": {
    "path": "/hooks/deploy",
    "method": "POST",
    "secret": "hmac-secret-key"
  }
}
```

### activate_trigger / deactivate_trigger

Enable or pause a trigger without deleting its configuration.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `trigger_id` | string | Yes | — | Trigger ID |

### delete_trigger

Permanently remove a trigger.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `trigger_id` | string | Yes | — | Trigger ID |

### list_triggers

List all triggers with optional filters.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `status` | string | No | `null` | Filter by state |
| `trigger_type` | string | No | `null` | Filter by type |

### get_trigger

Get full trigger details by ID.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `trigger_id` | string | Yes | — | Trigger ID |

---


## Trigger Types

| Type | Watcher | Description |
|------|---------|-------------|
| `temporal` | TemporalWatcher | Cron expressions, intervals |
| `filesystem` | SystemWatcher | File/directory change events |
| `process` | SystemWatcher | Process start/stop/crash |
| `resource` | SystemWatcher | CPU/memory/disk threshold |
| `composite` | CompositeWatcher | AND/OR combination of other triggers |
| `iot` | IoTWatcher | GPIO pin state changes |

---


## Implementation Notes

- Uses TriggerDaemon component injected at startup
- Triggers stored in SQLite (`trigger.db_path`)
- Trigger chaining: one trigger can fire a plan that contains another trigger registration (max depth configurable: `trigger.max_chain_depth`)
- Conflict detection: prevents overlapping triggers from causing resource contention
- Resource locks: mutual exclusion across triggers sharing the same resource
