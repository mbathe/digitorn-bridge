---
id: hook-action-shell
title: "Hook action: shell"
type: hook-action
action: shell
keywords: [shell, action, hook, command, cwd, timeout, on_error]
---

# Hook action: `shell`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("shell")`.

## Params
| Param | Requirement |
|-------|-------------|
| `command` | required |
| `cwd` | optional |
| `on_error` | optional |
| `timeout` | optional |

## Behavior
Execute a shell command — routed through the `shell.bash` module
action so it INHERITS THE APP'S SECURITY PROFILE: requires the shell
module to be declared + granted, respects `shell.blocked_commands`,
runs under the workspace sandbox, honors max_risk_level.

Previously this action ran `asyncio.create_subprocess_shell()` directly
with no sandbox, no grant check, no path restriction — any app could
exfiltrate data, touch system files, or run arbitrary commands just
by adding a YAML hook. That was a configuration-driven sandbox
escape. Fixed by delegating to `shell.bash` through
`context_builder.execute_tool`, which enforces
`SecurityProfile.module_grants` + `ActionSpec.policy_decision`.

Params:
    command (str): Shell command to execute. Supports {{tool.*}}.
    timeout (float): Command timeout in seconds. Default: 30
    inject_result (bool): Inject stdout as system message. Default: false
    on_error (str): "ignore" or "inject". Default: "ignore"

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: shell
      # params: command, cwd, timeout, on_error
```
