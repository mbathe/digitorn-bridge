---
id: advanced-18-hook-chain
title: "Advanced 18 - Composing hook primitives around a tool"
sidebar_label: "Advanced 18: Hook composition"
---

The Hooks V2 engine exposes a small set of primitive actions
that compose into non-trivial workflows without writing any
middleware. This tutorial wires two primitives around a single
tool surface (`shell.bash`):

- **`transform_params`** on `tool_start` to inject a default
  parameter (`timeout: 10`) when the agent omits it.
- **`transform_result`** on `tool_end` to add a system note
  visible to the agent on its next turn.

## What you build

| Hook | Event | Action | Observable evidence |
|---|---|---|---|
| `bash_default_timeout` | `tool_start` | `transform_params: set: {timeout: 10}` | Tool params include `timeout: 10` even though the user prompt did not request it |
| `bash_trace_note` | `tool_end` | `transform_result: inject_note: "..."` | A system message appears in the conversation right after every bash tool_result |

## The YAML

```yaml
app:
  app_id: tuto-hook-chain
  name: Tuto - Hook Chain
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 4
  timeout: 60
  tool_injection: direct
  direct_modules: [shell, memory]
  hooks:
    # Pre-tool: inject a default timeout into every bash call.
    # transform_params runs ONLY on pre_tool_use (tool_start) events.
    - id: bash_default_timeout
      'on': tool_start
      condition:
        type: tool_name
        match: [bash, shell.bash]
      action:
        type: transform_params
        transformation:
          set:
            timeout: 10

    # Post-tool: inject a system note after every bash result.
    # We use inject_note (the system-message branch of
    # transform_result) rather than append_to_result because
    # bash returns a structured dict (stdout/stderr/exit_code);
    # mutating that into a stringified dict + footer confuses
    # tool-calling models into emitting malformed retries.
    - id: bash_trace_note
      'on': tool_end
      condition:
        type: tool_name
        match: [bash, shell.bash]
      action:
        type: transform_result
        transformation:
          inject_note: "Last bash command was traced by the hook chain (timeout default 10s applied)."

agents:
  - id: main
    role: assistant
    brain:
      provider: openai
      backend: openai_compat
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      temperature: 0.1
      max_tokens: 1024
    system_prompt: |
      You are a shell agent. Run the bash command the user
      requests. The runtime auto-traces every bash call.

tools:
  modules:
    shell: {}
    memory:
      config:
        working_memory: true
  capabilities:
    default_policy: auto
    max_risk_level: high
    grant:
      - module: shell
        actions: [bash]
      - module: memory
        actions: [remember, set_goal]
```

Three YAML rules to know:

- **Quote `'on':`** in the hook block. YAML 1.1 parses bare
  `on` as a boolean, which makes the compiler reject the hook
  (the field becomes `True: tool_end` instead of
  `'on': tool_end`).
- `transformation:` is the documented nested form. Both
  `transform_params` and `transform_result` accept it. The
  legacy flat form (`set:` at top level) still works for
  backward compatibility.
- `match:` accepts a list of tool names in either short
  (`bash`) or FQN (`shell.bash`) form. Listing both is
  defensive.

## Deploy and run

```bash
digitorn dev deploy tuto-hook-chain.yaml
digitorn dev chat tuto-hook-chain -m 'Run echo "hello digitorn" via the bash tool. Then paste the EXACT tool output you received.'
```

## Sample flow

**Hook 1: `transform_params` injects `timeout: 10`.**

The user did not mention timeout. The agent's tool call
nonetheless includes it:

```json
{
  "command": "echo \"hello digitorn\"",
  "description": "Run echo to print hello digitorn",
  "run_in_background": false,
  "timeout": 10
}
```

The `timeout: 10` field was added by `transform_params` on
`tool_start`, before the bash module saw the call.

**Hook 2: `transform_result.inject_note` adds a system note
after the tool result.**

Right after each `tool_result` event for bash, a
`system_message` event appears with the configured note:

```
tool_call       Bash(...)
system_message  "Last bash command was traced by the hook chain (timeout default 10s applied)."
```

The agent sees this system message on its next turn,
identical to a `system_prompt` segment.

## Choosing between `inject_note` and `append_to_result`

`transform_result` has two branches:

| Branch | What it mutates | Safe for |
|---|---|---|
| `inject_note: "..."` | Adds a `system_message` event to the conversation | Any tool, any result shape |
| `append_to_result: "..."` | Converts the tool result to `str(result) + "\n" + value` | Tools whose result is already a string |

`append_to_result` is convenient for tools that return plain
text. For tools that return structured dicts (bash, workspace,
filesystem), the string concatenation serialises the dict into
a less parseable form. On smaller tool-calling models this can
cause malformed follow-up tool calls; `inject_note` sidesteps
the issue entirely.

## Other primitives in the toolbox

The Hooks V2 engine ships 13 action types. The ones not
exercised in this tutorial:

- **`chain`** runs a list of actions sequentially. Useful when
  you want several side effects on one event.
- **`pipe`** routes the current tool's output into another
  tool. Example pattern: pipe every `bash` result into
  `memory.remember` for an auto-trace.
- **`gate`** blocks the tool call with a reason. The behavior
  engine's `action: block` is a higher-level version of the
  same idea; see [Advanced 17](advanced-17-gate-destructive.md).
- **`lsp_diagnose`** runs the LSP module's `notify_change` on
  any write-like tool and injects the diagnostics into the
  result. See [Advanced 16](advanced-16-selfcorrect-builtin.md)
  for the inline workspace-side equivalent.
- **`compact_context`**, **`module_action`**,
  **`module_action_inject`**, **`shell`**, **`log`**,
  **`notify`**: covered in the [Hooks reference](../reference/runtime/hooks.md).

## When to reach for this

- Defaults that the agent keeps forgetting (timeouts,
  working directories, locale flags). One `transform_params`
  hook makes the default unforgettable.
- Audit trails. Inject a `system_message` after every
  sensitive tool call so the next turn carries proof of what
  ran.
- Lightweight policy. A `transform_params.remove` hook can
  strip a dangerous flag (e.g. `--force`) without blocking
  the call outright.

Hooks are NOT the right tool for:

- Cross-turn pipelines where step B genuinely needs to wait
  for step A to land in chat. Use sub-agents
  ([Advanced 15](advanced-15-parallel-spawn.md)) or the
  agent loop itself.
- User-facing decision points. The agent should call
  `AskUser`, not the hook.
- Anything that mutates the model's response text. Hooks
  fire around tool calls, not LLM completions.
