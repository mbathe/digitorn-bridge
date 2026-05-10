---
id: advanced-09-bgrun
title: "Advanced 9 - The background_run primitive"
sidebar_label: "Advanced 9: background_run"
---

A normal tool call **blocks** the agent loop. The model fires
`Bash("npm run build")`, the daemon waits for npm to finish, the
turn doesn't advance until the result lands. For a 30-second
build that's fine; for a 5-minute install or a long-running
analysis it's a problem - the LLM session ties up a worker the
whole time.

**`background_run`** is the primitive that decouples a tool
launch from its completion. The agent fires the tool with
`background_run(name="Bash", params={...})`, the runtime
returns a `task_id` immediately, the tool keeps running in a
background worker, and the agent can poll status, wait, or
cancel later.

The five operating modes:

| Mode               | Call shape                                        | Returns                            |
|--------------------|---------------------------------------------------|------------------------------------|
| Launch             | `background_run(name="Bash", params={…})`         | `{task_id, status: "running"}`     |
| Status check       | `background_run(task_id="abc")`                   | `{task_id, status, elapsed_seconds}` |
| Wait for one       | `background_run(task_id="abc", wait=true, timeout=30)` | `{task_id, status, result}`   |
| Cancel             | `background_run(task_id="abc", cancel=true)`      | `{cancelled: true}`                |
| List all           | `background_run(list_tasks=true)`                 | List of every active task          |

The launch mode is the canonical "fire and continue" pattern.
Wait + status give the agent the building blocks for any flow
control - poll until done, race two background tasks, give up
after N seconds.

## The YAML

Save as `bg-bot.yaml`. The agent gets `shell.bash` plus the
`background_run` primitive (granted explicitly because it's a
meta-action filtered out of the auto-grant set).

```yaml
app:
  app_id: bg-bot
  name: Background Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 6
  timeout: 60

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
      max_tokens: 300
    system_prompt: |
      You can launch long-running shell commands in the background
      using background_run(name='Bash', params={...}, wait=true).
      Setting wait=true blocks until completion. Reply concisely
      with the captured output.

tools:
  modules:
    shell: {}
  capabilities:
    default_policy: auto
    max_risk_level: high
    grant:
      - module: shell
        actions: [bash]
      - module: context_builder
        actions: [background_run]
```

## Live transcript

The user asks the agent to run a 2-second sleep + echo in
background and wait for the output. Real session captured:

```text
> Run this in background and wait for completion:
  bash -c "sleep 2 && echo COMPLETED_AFTER_2S".
  Use background_run with wait=true.

Done! The background task completed successfully with:
- Exit code: 0
- Stdout: COMPLETED_AFTER_2S
- Duration: ~2.3 seconds
```

The session log shows **three tool calls** (`tool_calls_count: 3`):

```python
# 1. Launch in background
background_run(
  name="Bash",
  params={"command": "bash -c \"sleep 2 && echo COMPLETED_AFTER_2S\"",
          "run_in_background": True}
)
# → {task_id: "b5b113f5c094", status: "running", elapsed_seconds: 0}

# 2. Wait for completion
background_run(task_id="b5b113f5c094", wait=True, timeout=30)
# → {task_id: "b5b113f5c094", status: "completed", elapsed_seconds: 2.3}

# 3. Fetch the actual stdout via Bash status mode
Bash(task_id="48ae4330afa9")
# → {task_id, status: "finished", exit_code: 0,
#    stdout: "COMPLETED_AFTER_2S", uptime_seconds: 2.3}
```

The agent followed the right pattern: launch → wait →
fetch-output. The model figured out by itself that it needed a
final `Bash(task_id=...)` to actually grab stdout - the
`background_run` wait mode returns task metadata, not the stdout
of the underlying tool.

## Why decouple

A normal `Bash("sleep 2")` call holds the agent loop for 2
seconds. With background_run, the agent gets the task_id back
in milliseconds and can:

- **Run other work in parallel**. Launch the build, then in the
  same turn start a `Glob` to find files, then come back to the
  build with `wait`.
- **Time out gracefully**. `wait=true, timeout=10` blocks for
  at most 10 seconds; if the task isn't done, the agent
  decides to keep waiting, cancel, or do something else.
- **Hand back to the user**. The agent fires the build, replies
  "Build started, task_id=abc, I'll be here when you need
  status." The user keeps chatting; later they ask "is it
  done?" and the agent calls
  `background_run(task_id="abc")` to check.
- **Spawn one task that **survives** turn boundaries**. A long
  analysis can outlive the conversation that started it; the
  task_id is the handle.

## Cancellation and listing

The agent (or a hook) can call:

```python
background_run(task_id="abc", cancel=true)
# → {cancelled: true}

background_run(list_tasks=true)
# → [
#     {task_id: "abc", name: "Bash", status: "running", elapsed_seconds: 12.4},
#     {task_id: "def", name: "WsWrite", status: "completed", elapsed_seconds: 0.3},
#   ]
```

Cancellation is **cooperative** for module actions but **hard**
for shell - a Bash background task gets a real `SIGKILL` if it
doesn't terminate after a 1-second SIGTERM grace period. The
session-end abort path also cancels every task that's still
alive at the moment the session closes.

## Compose with hooks

`background_run` pairs cleanly with hooks. A
`tool_end` hook on the launch mode can auto-record the
task_id into memory; a periodic hook can poll every active
background task and notify when one completes.

```yaml
runtime:
  hooks:
    - id: remember_bg_tasks
      "on": tool_end
      condition:
        type: tool_name
        match: background_run
      action:
        type: pipe
        to: memory.remember
        map:
          content: "Background task launched: {{tool.result.task_id}} ({{tool.params.name}})"
        on_error: ignore
```

## Going further

- The full primitive reference (every mode, every parameter,
  the cancellation guarantees):
  [Built-in tools - background_run](../language/04b-builtin-tools.md).
- The companion **`run_parallel`** primitive that fires N
  tools in true parallelism (asyncio.gather):
  [Execution primitives](../language/04c-primitives.md).
- For tool-output **chaining** as a related but distinct
  pattern: [Advanced 7 - Hooks pipe](advanced-07-hooks-pipe.md).
