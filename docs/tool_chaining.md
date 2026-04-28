# Tool Chaining - Runtime Primitive

> Route the output of **any** tool (native module or MCP server) into
> **any** other tool. Zero Python code, pure YAML. Works the same whether
> the upstream tool is `filesystem.write`, `mcp.github.create_pr`, or a
> custom module an app-dev wrote yesterday.

This is the single most important feature for building real workflows on
Digitorn. Read this once, then build anything.

## The 10-second mental model

1. **A hook fires** on `tool_end` (or any other event).
2. **The hook action** (`pipe`, `module_action`, `shell`, …) sees the
   upstream tool's input, output, and metadata via a shared template
   syntax.
3. **It calls a downstream tool** with params computed from that data.

Example - turn a GitHub PR fetch into a Slack notification:

```yaml
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
```

That's the whole feature. The rest of this doc is details.

## The placeholder syntax

Every hook action's string / dict / list parameter is scanned recursively
and `{{...}}` placeholders are resolved from the upstream tool's context:

| Placeholder | Returns |
|---|---|
| `{{tool.name}}`   | Short name of the tool (e.g. `"create_pr"`) |
| `{{tool.fqn}}`    | Fully-qualified name (`"mcp.github.create_pr"`) |
| `{{tool.params.X}}` | Tool input param `X` - supports dotted paths |
| `{{tool.result.X}}` | Tool output field `X` - same syntax |
| `{{tool.result}}` | The entire result serialized as JSON |
| `{{tool.error}}`  | Error string, or `""` when the tool succeeded |

### Path syntax

Applies to both `{{tool.params.X}}` and `{{tool.result.X}}`:

```text
user.login                   # dict access
files.0.path                 # list index (0-based)
response.hits.-1.id          # negative index = from end
deeply.nested.3.metadata.tag # mix at any depth
```

**Safe navigation**: any missing segment renders as an empty string.
Templates never raise - if an MCP server changes its response shape,
your hook degrades gracefully to empty values instead of crashing the
agent turn.

## The `pipe` action

The clean API for the 90% case: route one tool's output into another.

```yaml
action:
  type: pipe
  to: <destination.tool>         # required
  map:                            # param name → template
    foo: "{{tool.result.bar}}"
    nested:
      deep: "{{tool.params.x}}"
  extra:                          # literal params (no templating)
    flag: true
  on_error: ignore                # ignore (default) | log | raise
```

- `map` is templated recursively - nested dicts and lists are walked.
- `extra` is merged into the final call as-is; nothing gets interpreted.
  Use it for booleans, integers, enums that would otherwise become
  strings through templating.
- `on_error` controls what happens when the **downstream** tool fails:
  - `ignore` (default) - swallow, log at debug.
  - `log` - warning-level log line.
  - `raise` - propagate, aborts an enclosing `chain`.

## Advanced composition with `chain`

Use `chain` to run multiple actions in order. Combine with `pipe`
for multi-step pipelines:

```yaml
action:
  type: chain
  stop_on_failure: true
  actions:
    - type: lsp_diagnose                # step 1: lint
      inject_result: true                # agent sees errors → can retry

    - type: pipe                         # step 2: if lint passed, deploy
      to: ci.trigger_build
      map:
        sha: "{{tool.result.commit.sha}}"
      on_error: raise                    # abort the chain on failure

    - type: pipe                         # step 3: notify
      to: slack.send_message
      map:
        channel: "#deploy"
        text: "Build queued for {{tool.result.commit.sha}}"
```

## Working with MCP tools

Every MCP tool flows through the exact same `tool_context` shape. The
tool name is always `mcp.<server_id>.<tool_name>`. Use the full FQN
in the `condition.tool_name` list and in the `pipe.to` field.

MCP tools often have irregular param names (`filepath` vs `file_path`,
`contents` vs `content`). Two defense mechanisms:

1. **Template the param name you need explicitly** - no magic guessing:
   ```yaml
   map:
     # The MCP tool uses `filepath`, but downstream expects `path`.
     path: "{{tool.params.filepath}}"
   ```

2. **Use `lsp_diagnose` for the specific post-write-lint case** - it
   takes a cascade of candidate field names, so one hook covers most
   MCP conventions without per-server tuning.

## Debugging

- `DIGITORN_LOGGING__LEVEL=debug` prints template resolution + downstream
  tool outcomes.
- Failed downstream calls appear in `state.metadata["hook_failures"]` as
  `{action, error}` entries. Later actions in the chain can inspect them.
- Test templates without shipping a real app:
  ```python
  from digitorn.core.runtime.hooks import _render_tool_templates
  from types import SimpleNamespace
  state = SimpleNamespace(tool_context=SimpleNamespace(
      tool_name="mcp.x.y",
      tool_params={"a": 1},
      tool_result={"b": {"c": [10, 20]}},
      tool_error=None,
  ))
  print(_render_tool_templates("b.c[1]={{tool.result.b.c.1}}", state))
  # → "b.c[1]=20"
  ```

## Patterns

### 1. Lint every file write - regardless of source

```yaml
hooks:
  - event: tool_end
    condition:
      type: tool_name
      value: ["filesystem.write", "filesystem.edit",
              "workspace.write", "workspace.edit",
              "mcp.github.create_or_update_file"]
    action:
      type: lsp_diagnose
      inject_result: true
```

### 2. Persist external data to memory

```yaml
hooks:
  - event: tool_end
    condition:
      type: tool_name
      value: ["mcp.notion.get_page"]
    action:
      type: pipe
      to: memory.remember
      map:
        key: "notion:{{tool.params.page_id}}"
        value: "{{tool.result.title}} - {{tool.result.last_edited}}"
```

### 3. Push search results to a preview channel

```yaml
hooks:
  - event: tool_end
    condition:
      type: tool_name
      value: ["mcp.search.query"]
    action:
      type: pipe
      to: preview.set_resource
      map:
        channel: search_results
        id: "{{tool.params.query}}"
        payload:
          hits: "{{tool.result.hits}}"
          took: "{{tool.result.took_ms}}"
```

### 4. Forward tool errors as user-facing notifications

```yaml
hooks:
  - event: tool_end
    condition:
      type: tool_failed
    action:
      type: pipe
      to: notify
      map:
        title: "Tool failed: {{tool.fqn}}"
        message: "{{tool.error}}"
        level: error
```

### 5. Trigger a build on commit - stop the chain on lint failure

```yaml
hooks:
  - event: tool_end
    condition:
      type: tool_name
      value: ["mcp.github.create_commit"]
    action:
      type: chain
      stop_on_failure: true
      actions:
        - type: lsp_diagnose
          inject_result: true
        - type: pipe
          to: mcp.ci.trigger_build
          map:
            ref: "{{tool.result.sha}}"
          on_error: raise
```

## When NOT to chain

- **Multi-step LLM reasoning** - use sub-agents (`Agent(prompt=...)`),
  not hooks. Hooks are deterministic.
- **User-facing approval flows** - hooks can't block for interactive
  input. Use the `capabilities.approve` policy + `ApprovalQueue`.
- **Heavy computation** - hooks run inline on the turn's event loop.
  For long tasks (>2 s), chain into a background task via
  `module_action` on `agent_spawn.spawn`.

## Registered by default

These hook actions all support the full template syntax described here:

- `pipe` - the main one.
- `module_action` / `module_action_inject` - low-level alternatives.
- `shell` - for system commands.
- `lsp_diagnose` - specialized post-write LSP trigger.
- `transform_result` - for inline result modification.

## Reference

- Source: `packages/digitorn/core/runtime/hooks.py`
- Tests: `tools/behavior_tests.py::PIPE01` + `HK01/HK02`.
- Related docs: [hooks.md](hooks.md), [RULES_MATRIX.md](RULES_MATRIX.md).
