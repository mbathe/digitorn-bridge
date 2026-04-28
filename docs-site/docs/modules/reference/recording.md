---

id: recording
title: Recording Module
sidebar_position: 5
format: md
---




# recording

Workflow recording and replay. Capture sequences of plan executions, then replay them as new plans. Implements Shadow Recording (Phase A).

| Property | Value |
|----------|-------|
| **Module ID** | `recording` |
| **Version** | `1.0.0` |
| **Type** | system |
| **Platforms** | All |
| **Dependencies** | None |
| **Configuration** | `recording.enabled = true` (disabled by default) |

---


## Actions (6)

### start_recording

Begin a named recording session. All plans submitted during this session are captured.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title` | string | Yes | - | Recording title |
| `description` | string | No | `""` | Description |

**Returns**: `{"recording_id": "rec-001", "status": "recording"}`

**Security**: `@audit_trail("standard")`

### stop_recording

Stop the recording session and generate a replay plan.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `recording_id` | string | Yes | - | Recording to stop |

**Returns**: `{"recording_id": "rec-001", "status": "stopped", "plan_count": 5, "replay_plan": {...}}`

**Security**: `@audit_trail("standard")`

### list_recordings

List all recordings with optional status filter.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `status` | string | No | `null` | Filter by status: `recording`, `stopped` |

### get_recording

Get recording details including captured plans and replay plan.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `recording_id` | string | Yes | - | Recording ID |

**Security**: `@data_classification(DataClassification.INTERNAL)`

### generate_replay_plan

Regenerate the replay plan for a stopped recording.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `recording_id` | string | Yes | - | Recording ID |

**Security**: `@audit_trail("standard")`

### delete_recording

Permanently delete a recording.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `recording_id` | string | Yes | - | Recording ID |

**Security**: `@sensitive_action(RiskLevel.MEDIUM)`

---


## Implementation Notes

- Requires `recording.enabled = true` in configuration
- Uses the WorkflowRecorder component injected at startup
- Recordings stored in SQLite database (`recording.db_path`)
- Replay plans are IML v2 plans that can be submitted back to the daemon
- Plans captured during recording preserve dependencies and template references
