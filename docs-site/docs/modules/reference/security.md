---

id: security
title: Security Module
sidebar_position: 4
format: md
---




# security

OS-level permission and audit management. Provides IML actions for listing, checking, requesting, and revoking permissions. Also exposes the security status overview.

| Property | Value |
|----------|-------|
| **Module ID** | `security` |
| **Version** | `1.0.0` |
| **Type** | system |
| **Platforms** | All |
| **Dependencies** | None |
| **Declared Permissions** | `admin` |

---


## Actions (6)

### list_permissions

List all currently granted permissions.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `module_filter` | string | No | `null` | Filter by module ID |
| `scope` | string | No | `null` | Filter by scope: `session` or `permanent` |

**Returns**:
```json
{
  "permissions": [
    {
      "permission": "filesystem.write",
      "module_id": "filesystem",
      "scope": "session",
      "granted_by": "auto_grant",
      "granted_at": "2024-01-15T10:00:00Z",
      "reason": "Auto-granted LOW risk permission"
    }
  ],
  "total": 1
}
```

**Security**: `@data_classification(DataClassification.INTERNAL)`

### check_permission

Check if a specific permission is granted for a module.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `permission` | string | Yes | - | Permission string (e.g., `"filesystem.write"`) |
| `module_id` | string | No | `null` | Module context |

**Returns**: `{"granted": true, "scope": "session", "reason": "..."}`

### request_permission

Request a new permission. In Phase 1, LOW risk permissions are auto-granted when `auto_grant_low_risk = true`.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `permission` | string | Yes | - | Permission to request |
| `module_id` | string | Yes | - | Requesting module |
| `reason` | string | Yes | - | Justification |
| `scope` | string | No | `"session"` | `session` or `permanent` |
| `risk_level` | string | No | `"low"` | `low`, `medium`, `high`, `critical` |

**Security**:
- `@requires_permission(Permission.ADMIN)`
- `@sensitive_action(RiskLevel.HIGH)`
- `@audit_trail("detailed")`

### revoke_permission

Revoke a previously granted permission.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `permission` | string | Yes | - | Permission to revoke |
| `module_id` | string | No | `null` | Module context |
| `scope` | string | No | `null` | Scope to revoke from |

**Security**:
- `@requires_permission(Permission.ADMIN)`
- `@sensitive_action(RiskLevel.HIGH)`
- `@audit_trail("detailed")`

### get_security_status

Get a comprehensive security overview.

**Returns**:
```json
{
  "profile": "local_worker",
  "decorators_enabled": true,
  "rate_limiting_enabled": true,
  "auto_grant_low_risk": true,
  "grants_by_risk": {
    "low": 12,
    "medium": 3,
    "high": 1,
    "critical": 0
  },
  "grants_by_module": {
    "filesystem": 3,
    "os_exec": 2,
    "api_http": 4
  },
  "total_grants": 16,
  "scanner_pipeline_enabled": true,
  "intent_verifier_enabled": true
}
```

**Security**: `@data_classification(DataClassification.INTERNAL)`

### list_audit_events

List recent audit events (stub for Phase 3).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | integer | No | `50` | Maximum events to return |
| `module_filter` | string | No | `null` | Filter by module |

---


## Permission Scopes

| Scope | Behavior |
|-------|----------|
| `session` | Valid for current daemon session only. Cleared on restart. |
| `permanent` | Persisted to SQLite. Survives daemon restarts. |

---


## Permission Lifecycle

```
Request received
    |
    v
Risk assessment
    |
    +--→ LOW + auto_grant_low_risk → Auto-grant, emit audit event
    |
    +--→ MEDIUM/HIGH/CRITICAL → Phase 2: approval gate (not yet implemented)
    |
    v
Grant stored in PermissionStore (SQLite)
    |
    v
Future action calls → SecurityManager checks grant exists
```

---


## Implementation Notes

- Requires SecurityManager to be injected (`set_security()`)
- When decorators are disabled, this module is still registered but actions that require SecurityManager return error messages
- PermissionStore uses SQLite with lazy expiry cleanup
- 26+ built-in permission constants are extensible - community modules can define `"my_plugin.resource"` strings
