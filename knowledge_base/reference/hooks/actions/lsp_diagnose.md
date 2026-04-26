---
id: hook-action-lsp_diagnose
title: "Hook action: lsp_diagnose"
type: hook-action
action: lsp_diagnose
keywords: [lsp_diagnose, action, hook, path_field, content_field, publish, inject_result, read_from_disk]
---

# Hook action: `lsp_diagnose`

Registered in `packages/digitorn/core/runtime/hooks.py` via `@register_action("lsp_diagnose")`.

## Params
| Param | Requirement |
|-------|-------------|
| `content_field` | optional |
| `inject_result` | optional |
| `path_field` | optional |
| `publish` | optional |
| `read_from_disk` | optional |

## Behavior
Universal post-write LSP trigger — agnostic of which tool wrote
the file. Meant to be wired on ``tool_end`` so **any** module
(filesystem, workspace, a custom one, or an MCP server tool) gets
free diagnostics without code changes.

How it resolves (path, content) — in this order:

1. ``tool_params[path_field[i]]`` — try each configured key name in
   order. Default list covers Digitorn + common MCP conventions:
   ``["file_path", "path", "filepath", "filename", "file"]``.
2. ``tool_params[content_field[i]]`` — same cascade. Default:
   ``["content", "contents", "body", "text", "data"]``.
3. If content wasn't in params (e.g. the tool used ``old_string`` /
   ``new_string`` for an edit) AND ``read_from_disk=true`` (default),
   the action reads the current file content from disk — resolves
   relative paths against the session workspace.

Params:
    path_field       str | list[str] — param keys holding the path.
    content_field    str | list[str] — param keys holding content.
    read_from_disk   bool (default True) — fall back to reading the
                      file from disk when content isn't in params.
                      Critical for MCP tools that only return a
                      success status without echoing the content.
    publish          bool (default True) — push the result to the
                      ``diagnostics`` preview channel for clients.
    inject_result    bool (default False) — rewrite the tool's
                      ``tool_result`` so the agent's next turn sees
                      ``lint``, ``errors``, ``warnings`` fields on
                      the same response the MCP tool returned.
                      Enables the same self-correction loop the
                      workspace / filesystem modules give for free.

Example (MCP GitHub tool that writes a file)::

    hooks:
      - event: tool_end
        condition:
          type: tool_name
          value: ["mcp.github.create_or_update_file"]
        action:
          type: lsp_diagnose
          path_field: ["path", "file_path"]
          content_field: ["content"]
          inject_result: true

The action is a **no-op** when the tool doesn't match a write
pattern (no path found) — safe to register on broad conditions.

## YAML

```yaml compile=skip
hooks:
  - id: my-hook
    "on": turn_end
    condition:
      type: always
    action:
      type: lsp_diagnose
      # params: path_field, content_field, publish, inject_result, read_from_disk
```
