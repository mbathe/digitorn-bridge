---
id: hook-action-auto_test_deploy
title: "Hook action: auto_test_deploy"
type: hook-action
action: auto_test_deploy
keywords: [auto_test_deploy, action, hook, smoke_message, timeout, inject_result, only_on_deploy]
---

# Hook action: `auto_test_deploy`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("auto_test_deploy")`.

## Params
| Param | Requirement |
|-------|-------------|
| `inject_result` | optional |
| `only_on_deploy` | optional |
| `smoke_message` | optional |
| `timeout` | optional |

## Behavior
Post-deploy mandatory smoke test - makes Phase 6 non-skippable.

Wired on ``tool_end`` after ``dev_tools.app`` with ``deploy_draft_id``
set and a successful result. Sends a canonical smoke message through
``dev_tools.chat`` (watch mode), writes ``_state/deploy.json`` and
appends the outcome to ``_state/tests.json`` - the preview's auto-test
strip and readiness dashboard read both files to flip green.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: auto_test_deploy
      # params: smoke_message, timeout, inject_result, only_on_deploy
```
