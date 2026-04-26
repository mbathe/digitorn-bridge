---
id: hook-action-compile_yaml
title: "Hook action: compile_yaml"
type: hook-action
action: compile_yaml
keywords: [compile_yaml, action, hook, path_field, content_field, only_path, inject_result]
---

# Hook action: `compile_yaml`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("compile_yaml")`.

## Params
| Param | Requirement |
|-------|-------------|
| `content_field` | optional |
| `inject_result` | optional |
| `only_path` | optional |
| `path_field` | optional |

## Behavior
Post-write Digitorn-YAML validator — non-skippable compile loop.

Wired on ``tool_end`` after a write tool (typically ``workspace.write``
or ``workspace.edit`` on ``app.yaml``). Runs the daemon's
``dev_tools.app(compile_yaml=true)`` with the content the agent just
wrote and, when ``inject_result`` is set, merges any compile errors
back into the write tool's ``tool_result`` so the agent is forced
to see them before proceeding.

The Builder prompt already says "compile after every write". In
practice LLMs skip it. This hook turns a discipline rule into a
mechanical one: the agent physically cannot receive a "write ok"
response for ``app.yaml`` without also seeing the compile verdict.

Params:
    path_field       str | list[str] — param keys holding the path.
                      Default: ``["path", "file_path"]``.
    content_field    str | list[str] — param keys holding content.
                      Default: ``["content"]``.
    only_path        str — when set, the hook is a no-op unless the
                      written path matches. Use ``"app.yaml"`` on a
                      builder-style app to scope tightly.
    inject_result    bool (default True) — rewrite the write tool's
                      ``tool_result`` so the agent's next turn sees
                      ``compile.success`` / ``compile.errors``.

Example (in a builder app yaml)::

    hooks:
      - id: compile_on_app_yaml_write
        on: tool_end
        condition:
          all_of:
            - type: tool_name
              value: ["workspace.write", "workspace.edit"]
            - type: tool_failed
              negate: true
        action:
          type: compile_yaml
          only_path: "app.yaml"
          inject_result: true

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: compile_yaml
      # params: path_field, content_field, only_path, inject_result
```
