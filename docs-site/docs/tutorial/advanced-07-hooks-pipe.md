---
id: advanced-07-pipe
title: "Advanced 7 - Hooks pipe (tool chaining)"
sidebar_label: "Advanced 7: Pipe"
---

The behaviour engine adds rules around tool calls; middleware
wraps the LLM call. **Hooks v2** sit at a third spot - they fire
on **tool lifecycle events** and can modify, gate, or extend
each call as it happens. The hook action that does the most
work here is **`pipe`**: it routes one tool's output into
another tool **as soon as the first one finishes**, without the
agent ever asking.

This is real **tool chaining** in YAML. The agent runs Bash;
the daemon automatically writes the result to a log file. The
agent fetches a webhook payload; the daemon automatically
forwards it to Slack. The agent compiles a draft; the daemon
automatically deploys it. None of those involve the LLM
deciding to call the second tool - the pipe is the daemon's
job.

## How pipe works

A `pipe` action declares:

- **`to`** - the destination tool name (`module.action` or an
  MCP tool id)
- **`map`** - destination param names → templated values, with
  `{{tool.params.X}}` and `{{tool.result.X}}` placeholders
- **`extra`** - literal params that don't need templating
- **`on_error`** - `ignore` / `log` / `raise`

When the upstream tool finishes, the runtime renders the
template against the upstream's `tool_context`, calls the
destination tool with the rendered params + extras, and
discards the destination's return value (or logs the error
per `on_error`). The agent sees the upstream tool's result as
usual; the pipe is invisible to the LLM.

## The YAML

Save as `pipe-bot.yaml`. After **every** Bash call, the daemon
auto-writes the result to `audit-log.txt` in the workspace.
The agent runs Bash; it does not need to call Write.

```yaml
app:
  app_id: pipe-bot
  name: Pipe Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 4
  timeout: 90
  hooks:
    - id: log_bash_output
      "on": tool_end
      condition:
        type: tool_name
        match: bash
      action:
        type: pipe
        to: workspace.write
        map:
          path: "audit-log.txt"
          content: "{{tool.result}}"
        on_error: log

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      credential:
        ref: deepseek_main
        scope: per_user
        provider: deepseek
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: https://api.deepseek.com/v1
      temperature: 0
      max_tokens: 200
    system_prompt: |
      Run Bash commands as requested. Reply with one short
      confirmation. Do NOT call Write yourself; the hook handles it.

tools:
  modules:
    shell: {}
    workspace:
      config:
        render_mode: code
        entry_file: audit-log.txt
        title: Pipe Bot
        sync_to_disk: false
        lint: false
    preview: {}
  capabilities:
    default_policy: block
    max_risk_level: high
    grant:
      - module: shell
        actions: [bash]
      - module: workspace
        actions: [write, read]
      - module: preview
        actions: [set_state, get_state]
```

The system prompt explicitly tells the agent **not** to call
Write - the hook does it. This isolates the demo: any Write
that lands in `audit-log.txt` came from the pipe, not from the
agent's own decision.

## Live transcript

Sample transcript:

```text
> Run Bash: echo "hello pipe demo"

Done. Output: `hello pipe demo`
```

`tool_calls_count: 1` (only the Bash call - the pipe doesn't
count as an agent tool call).

After the turn, the workspace API returns `audit-log.txt` with
the auto-written content:

```text
ActionResult(success=True, data={'stdout': 'hello pipe demo\n',
'stderr': '', 'exit_code': 0, 'command': 'echo "hello pipe demo"',
'cwd': 'C:\\Users\\ASUS\\Documents\\digitorn-bridge',
'platform': 'windows', 'shell': 'C:\\Program Files\\Git\\bin\\bash.exe'},
error=None, metadata={})
```

The full ActionResult landed in the file because `{{tool.result}}`
serialises the whole object. For a clean stdout-only audit you
narrow with a path:

```yaml
content: "{{tool.result.data.stdout}}"
```

The `_walk_path` resolver walks `tool_result.data.stdout` and
emits just that field. Combine with `extra: {append: true}` (if
the destination tool supports it) to accumulate logs across
multiple Bash calls in the same file.

## What you can pipe to

The `to` field accepts any tool the daemon can dispatch:

- **Native modules**: `workspace.write`, `filesystem.write`,
  `memory.remember`, `channels.send_message`,
  `database.execute`.
- **MCP tools**: any tool exposed by a connected MCP server,
  by its full id.
- **Other agent actions**: `agent_spawn.spawn` to fire a
  sub-agent on the upstream tool's result.

The agent's capability profile is consulted when the pipe
runs - the destination tool must be reachable by the pipe's
runtime profile (typically the same as the agent's). Setting
the destination to a tool the agent can't grant doesn't bypass
gates; it just fails at gate-1 for the pipe's call.

## Other hook actions worth knowing

`pipe` is one of about a dozen hook actions. The most useful:

| Action               | Purpose                                                       |
|----------------------|---------------------------------------------------------------|
| `pipe`               | Chain one tool's output into another                          |
| `module_action`      | Call any module action (free-form), with templating           |
| `module_action_inject` | Call an action and inject the result into the agent's context |
| `inject_message`     | Add a system / user / assistant message into the loop         |
| `gate`               | Block the upstream tool call before it executes               |
| `transform_params`   | Rewrite the upstream tool's params before it runs             |
| `transform_result`   | Rewrite the upstream tool's result before the agent sees it   |
| `lsp_diagnose`       | Auto-lint after a write tool, with optional self-correction   |
| `compact_context`    | Summarise old turns when context pressure crosses a threshold |
| `shell`              | Run a shell command (templated)                                |
| `chain`              | Run multiple actions in sequence                               |
| `notify`             | Push a notification through the channels module               |

Each is wired the same way: `on:` an event, `condition:` a
predicate, `action:` one of the registered types. The full
schema (events, conditions, actions, cooldowns, max_fires,
priority) is in [Tool hooks](../language/31-tool-hooks.md).

## Hooks vs middleware vs behaviour

The three runtime layers feel similar but each operates on a
different scope:

| Layer            | Fires on                           | Scope                  | Typical use                       |
|------------------|------------------------------------|------------------------|-----------------------------------|
| Middleware       | LLM call                           | Per request to the model | Mask secrets, content filter, RAG inject |
| Hooks            | Tool call lifecycle (start / end)  | Per individual tool call | Pipe chaining, lint after write, gate     |
| Behaviour engine | Tool call (with rule evaluation)   | Per tool call (rules)  | "Read before edit", "no `rm -rf`"  |

Pick by where your trigger lives: **inside an LLM request** →
middleware. **Around a tool call** → hooks. **Pattern-based on
tool params** → behaviour. They compose, not compete.

## When to reach for pipe

- **Audit / mirror**: every shell call mirrored to a log; every
  database write mirrored to S3; every workspace edit pushed
  to git as a commit.
- **Notification**: every `channel.receive` from Slack auto-
  acknowledged; every error event auto-paged; every
  `payment_received` event auto-emailed.
- **Composite tools**: a tool that "fetches and stores" without
  the agent having to remember both steps. The pipe makes
  store-after-fetch the daemon's responsibility.

The pattern matters because the agent **cannot forget** the
chained step. A pipe that writes audit logs runs every time;
relying on the system prompt to tell the agent "always log
after Bash" loses about 5-10% of the time.

## Going further

- The full hooks v2 reference (events, conditions, actions,
  cooldowns, composability):
  [Tool hooks](../language/31-tool-hooks.md).
- The pipe-action page in the runtime reference, with every
  template placeholder and every error mode:
  [Tool chaining](../reference/runtime/tool-chaining.md).
- For chaining at a different layer (middleware around the
  LLM call): [Advanced 5 - Middleware](advanced-05-middleware.md).
