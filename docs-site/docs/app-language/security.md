---

id: security
title: Security
sidebar_position: 12
format: md
---


# Security

LLMOS provides two levels of security configuration: a simple `security:` shorthand and fine-grained `capabilities:` for advanced control.

## Security Profiles (Simple)

The `security:` block provides quick security configuration:

```yaml
security:
  profile: power_user
  sandbox:
    allowed_paths:
      - "{{workspace}}"
    blocked_commands:
      - "rm -rf /"
      - "dd if=/dev/zero"
      - "mkfs"
```

### Available Profiles

| Profile | Description | Typical Use |
|---------|-------------|-------------|
| `readonly` | Read-only access, no writes, no commands | Data analysis, Q&A |
| `local_worker` | Read/write in allowed paths, limited commands | Build tools, linters |
| `power_user` | Most operations allowed, some restrictions | Development assistants |
| `unrestricted` | Full access, no restrictions | Trusted environments only |

### Sandbox Fields

| Field | Type | Description |
|-------|------|-------------|
| `allowed_paths` | list[string] | Directories the app can access |
| `blocked_commands` | list[string] | Shell commands that are forbidden |

## Capabilities (Advanced)

The `capabilities:` block provides fine-grained security control with grants, denials, approvals, and audit.

### Grants

Explicitly grant access to modules and actions:

```yaml
capabilities:
  grant:
    - module: filesystem
      actions: [read_file, write_file, list_directory]
      constraints:
        paths: ["{{workspace}}"]
        max_file_size: "10MB"

    - module: os_exec
      actions: [run_command]
      constraints:
        timeout: "30s"
        forbidden_commands: ["rm -rf", "sudo"]

    - module: network
      actions: [http_request]
      constraints:
        allowed_domains: ["api.github.com"]
```

An empty `actions` list means all actions are granted:

```yaml
capabilities:
  grant:
    - module: filesystem              # All filesystem actions
    - module: os_exec
      actions: [run_command]          # Only run_command
```

### Denials

Explicitly deny access to specific actions:

```yaml
capabilities:
  deny:
    - module: filesystem
      action: delete_file
      reason: "File deletion not allowed in this app"

    - module: os_exec
      action: kill_process
      when: "{{agent.turn_count > 20}}"    # Conditional denial
      reason: "Process management disabled after 20 turns"
```

### Denial Fields

| Field | Type | Description |
|-------|------|-------------|
| `module` | string | Module to deny |
| `action` | string | Specific action (empty = all) |
| `when` | string | Condition expression (empty = always) |
| `reason` | string | Human-readable reason |

### Approval Gates

Require human approval before certain actions:

```yaml
capabilities:
  approval_required:
    # Approve any file deletion
    - module: filesystem
      action: delete_file
      message: "Allow deleting {{params.path}}?"
      timeout: "300s"
      on_timeout: reject              # approve | reject | skip
      channel: cli                    # cli | http | slack | email

    # Approve after N actions
    - trigger: action_count
      threshold: 50
      message: "Agent has performed 50 actions. Continue?"
      on_timeout: reject

    # Conditional approval (note: os_exec.run_command takes command as a list)
    - module: os_exec
      action: run_command
      when: "{{params.command | join(' ') | startswith('git push')}}"
      message: "Allow git push?"
```

### Approval Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `module` | string | `""` | Module requiring approval |
| `action` | string | `""` | Action requiring approval |
| `when` | string | `""` | Condition (empty = always) |
| `message` | string | `""` | Message shown to approver |
| `timeout` | string | `"300s"` | Approval timeout |
| `on_timeout` | enum | `"reject"` | `approve`, `reject`, or `skip` |
| `channel` | string | `"cli"` | Approval channel |
| `trigger` | string | `""` | Count-based trigger (`"action_count"`) |
| `threshold` | int | `0` | Count threshold |

### Approval Runtime Pipeline

When the daemon processes a tool call with approval rules, the following happens:

1. **DaemonToolExecutor** checks `capabilities.approval_required` rules
2. If a rule matches (module + action + `when` condition), execution pauses
3. An **approval gate** callback is invoked with the module, action, and params
4. The gate waits for a decision (up to `timeout`):
   - **CLI mode** - The user is prompted interactively in the terminal
   - **HTTP mode** - A pending approval is created at `GET /plans/{id}/pending-approvals`
5. The approver responds with a **rich decision**:
   - `approve` - Proceed with execution
   - `reject` - Block the action, return an error
   - `skip` - Skip this action, continue the agent loop
   - `modify` - Approve with modified parameters (e.g., change the file path)
   - `approve_always` - Approve this and all future matching actions
6. If timeout is reached, `on_timeout` determines the outcome

### Compiler Validation

The compiler validates approval rules at compile time when `module_info` is available:

- Unknown modules in `approval_required` generate an error
- Unknown actions for a known module generate an error
- This prevents typos from silently disabling approval gates

### Audit Configuration

Configure audit logging:

```yaml
capabilities:
  audit:
    level: full                       # none | errors | mutations | full
    log_params: true                  # Log action parameters
    redact_secrets: true              # Redact sensitive values
    notify_on:
      - event: error
        channel: log
      - event: security_violation
        channel: log
```

### Audit Levels

| Level | What's Logged |
|-------|---------------|
| `none` | Nothing |
| `errors` | Only errors and failures |
| `mutations` | Write operations + errors |
| `full` | All actions (default) |

## Combining Security and Capabilities

You can use both `security:` and `capabilities:` together. The `security:` profile sets the baseline, and `capabilities:` adds fine-grained overrides:

```yaml
security:
  profile: local_worker
  sandbox:
    allowed_paths: ["{{workspace}}"]

capabilities:
  grant:
    - module: network
      actions: [http_request]
      constraints:
        allowed_domains: ["api.github.com"]

  deny:
    - module: os_exec
      action: kill_process

  approval_required:
    - module: filesystem
      action: delete_file
      message: "Delete {{params.path}}?"

  audit:
    level: full
```

## Daemon Enforcement

When the daemon is running, `security.profile` is **actively enforced** at runtime. Every tool call passes through the `PermissionGuard`, which switches to the profile declared in your YAML:

```text
App tool call
    → DaemonToolExecutor
        → PermissionGuard.set_profile("power_user")   ← from YAML
        → PermissionGuard.check_action()               ← enforced
        → SecurityScanner.scan()                        ← prompt injection check
        → ModuleRegistry.get(module).execute()
        → OutputSanitizer.sanitize()
        → EventBus.emit("llmos.actions.results")       ← audit trail
```

In **standalone mode** (no daemon), the profile declaration is stored but not enforced - only `filesystem` and `os_exec` are available with basic protections.

### Profile Enforcement Examples

```yaml
# This profile blocks write operations:
security:
  profile: readonly

agent:
  tools:
    - module: filesystem
      action: write_file    # Will be DENIED at runtime by PermissionGuard
```

```yaml
# Power user can do most things except a few restricted operations:
security:
  profile: power_user
  sandbox:
    allowed_paths: ["{{workspace}}"]
    blocked_commands: ["rm -rf /", "sudo"]
```

## Application Identity Integration

When a YAML app is **registered** with the daemon (`POST /apps/register` or `llmos app register`), an `Application` identity entity is **automatically created** with the same ID. This links the YAML app to the daemon's identity and RBAC system.

The auto-created Application identity extracts security constraints from your YAML:

| YAML Source | Identity Field |
| ----------- | -------------- |
| `agent.tools[].module` | `allowed_modules` |
| `agent.tools[].action` | `allowed_actions` (per module) |
| `app.max_concurrent_runs` | `max_concurrent_plans` |
| `app.max_actions_per_turn` | `max_actions_per_plan` |

### What This Means

- **Modules** your YAML declares in `tools:` become the `allowed_modules` whitelist
- **Actions** your YAML declares become the `allowed_actions` per-module whitelist
- If you declare `- module: filesystem` (all actions), all filesystem actions are allowed
- If you declare `- module: filesystem` with `actions: [read_file]`, only `read_file` is allowed

### Viewing the Linked Identity

The dashboard's **App Detail** page shows the linked Application identity:

- **Allowed Modules** - Which modules the app can access
- **Allowed Actions** - Which actions per module
- **Limits** - Max concurrent plans, max actions per plan
- **Link** - Click "View Full Identity" to manage sessions, API keys, and RBAC

You can also view it via the API:

```http
GET /applications/{app_id}
```

### Overriding via Dashboard

Administrators can modify the linked Application identity through the dashboard or API to:

- Add or remove allowed modules
- Restrict or expand allowed actions
- Create time-limited sessions with additional constraints
- Set permission grants/denials at the session level

These overrides take precedence over the YAML declarations. The YAML `capabilities.grant` declarations declare what the app *wants*; the daemon's Application identity controls what it *gets*.

## Security Best Practices

1. **Always set a profile** - Don't rely on defaults
2. **Restrict paths** - Use `sandbox.allowed_paths` to limit filesystem access
3. **Block dangerous commands** - Add `rm -rf`, `sudo`, `dd` to `blocked_commands`
4. **Use approval gates** - For destructive operations (delete, push, deploy)
5. **Enable audit** - Use `level: full` for production apps
6. **Principle of least privilege** - Grant only what's needed
