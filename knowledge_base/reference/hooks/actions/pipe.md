---
id: hook-action-pipe
title: "Hook action: pipe"
type: hook-action
action: pipe
keywords: [pipe, action, hook, to, map, extra, on_error]
---

# Hook action: `pipe`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("pipe")`.

## Params
| Param | Requirement |
|-------|-------------|
| `extra` | optional |
| `map` | optional |
| `on_error` | optional |
| `to` | required |

## Behavior
Chain the current tool's output into another tool.

The **primitive** for building tool pipelines in YAML. Works for any
writer: native modules, MCP tools, custom modules - as long as the
trigger hook is on a tool_start / tool_end event (so the upstream
tool's ``tool_context`` is available).

Params:
    to            str - destination tool name (``module.action`` or MCP
                   tool id). Required.
    map           dict - destination param name → template reference.
                   Values are rendered with the same ``{{tool.*}}``
                   placeholders as ``module_action``.
    extra         dict - literal params merged into the destination
                   call (no templating). Useful for static flags.
    on_error      "ignore" (default) | "log" | "raise". Controls what
                   happens when the downstream tool fails.

Example - pipe a GitHub fetch into Slack with field extraction::

    hooks:
      - event: tool_end
        condition:
          type: tool_name
          value: ["mcp.github.get_pull_request"]
        action:
          type: pipe
          to: mcp.slack.send_message
          map:
            channel: "#dev"
            text: "PR #{{tool.result.number}} - {{tool.result.title}} by {{tool.result.user.login}}"
          extra:
            as_user: true

Example - run LSP on any MCP file write::

    hooks:
      - event: tool_end
        condition:
          type: tool_name
          value: ["mcp.github.create_or_update_file"]
        action:
          type: pipe
          to: lsp.notify_change
          map:
            path: "{{tool.params.path}}"
            content: "{{tool.params.content}}"

Example - extract a nested array element::

    map:
      user_id: "{{tool.result.hits.0.user.id}}"

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: pipe
      # params: to, map, extra, on_error
```
