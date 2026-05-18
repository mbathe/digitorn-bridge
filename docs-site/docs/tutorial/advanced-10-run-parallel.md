---
id: advanced-10-parallel
title: "Advanced 10 - Parallel tool execution with run_parallel"
sidebar_label: "Advanced 10: run_parallel"
---

[Tutorial 4](04-multi-agent.md) showed how to spawn parallel
**sub-agents**. That's the right pattern when the work needs
distinct system prompts or specialised brains. For the simpler
case - **fire N independent tool calls and wait for all of
them** - there's a lighter primitive: **`run_parallel`**.

The agent passes a list of `{name, params}` tuples; the runtime
fires every call concurrently with `asyncio.gather`, collects
the results, and returns them as a single bundle. One tool call
in the agent's view, N parallel calls internally.

## When to use it

- **Fan-out reads**: search 5 different sources for the same
  query, merge the results.
- **Independent shell commands**: run `git status`, `git
  diff --stat`, `git log -3` in parallel and present a
  composite summary.
- **Bulk fetch**: `http.get` against 10 URLs at once instead of
  serializing them.
- **Validation across files**: lint 20 files concurrently,
  collect errors.

The constraint: the calls must be **independent**.
If call B needs the result of call A, you want sequential
chaining (a normal agent loop) or a hook pipe
([Advanced 7](advanced-07-hooks-pipe.md)), not parallel
fan-out.

## The YAML

Save as `parallel-bot.yaml`. The agent gets `shell.bash` plus
the `run_parallel` primitive.

```yaml
app:
  app_id: parallel-bot
  name: Parallel Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 4
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
      max_tokens: 400
    system_prompt: |
      You can execute multiple tool calls in parallel using
      run_parallel. Pass `actions` as a list of {name, params}
      dicts. The runtime fires them concurrently and returns
      every result. Use it when several independent calls can
      run at once. Reply with one short summary line.

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
        actions: [run_parallel]
```

`run_parallel` is a meta-action exposed by `context_builder`
(auto-loaded). The explicit `grant` is required because the
default tool index filters meta-actions out unless they're
named.

## Live transcript

The user asks the agent to fire three Bash echos in parallel.
Sample transcript:

```text
> Use run_parallel to fire THREE Bash calls at once:
  (1) echo ONE; (2) echo TWO; (3) echo THREE.
  Pass them as the calls list. Then list each stdout.

All three ran in parallel (~158ms). Here are the stdout results:

1. echo ONE   -> ONE
2. echo TWO   -> TWO
3. echo THREE -> THREE

All exited with code 0, no errors.
```

`tool_calls_count: 1` from the agent's perspective. The single
tool call was:

```python
run_parallel(
  actions=[
    {"name": "bash", "params": {"command": "echo ONE"}},
    {"name": "bash", "params": {"command": "echo TWO"}},
    {"name": "bash", "params": {"command": "echo THREE"}},
  ]
)
# returned:
# {
#   "results": [
#     {"name": "shell.bash", "success": true, "result": {...}},
#     {"name": "shell.bash", "success": true, "result": {...}},
#     {"name": "shell.bash", "success": true, "result": {...}},
#   ],
#   "total": 3,
#   "succeeded": 3,
#   "failed": 0,
#   "elapsed_ms": 157.8,
# }
```

Three Bash invocations, **158 ms total** for the parallel
batch. Sequential `Bash → Bash → Bash` on Windows takes
roughly 1.5 s for the same workload (Git Bash startup
amortises to ~500 ms per call). The 10x speedup comes from
the calls actually running concurrently in the asyncio
event loop.

## Anatomy of the result

The `results` list mirrors the input `actions` list **in
order**. Each entry has:

- **`name`** - the resolved FQN (`shell.bash`, not the short
  alias the agent used)
- **`success`** - `true` / `false`
- **`result`** - the tool's `data` dict on success
- **`error`** - the error message on failure

The aggregate fields (`total`, `succeeded`, `failed`,
`elapsed_ms`) let the agent quickly check the batch outcome
without iterating. A useful pattern is "fire 10 things, only
report the failed ones":

```text
result = run_parallel(actions=[...])
if result.failed > 0:
    failed = [r for r in result.results if not r.success]
    summarize_for_user(failed)
```

## Failure mode

`run_parallel` does **not** abort on the first failure - all N
calls are awaited. A single failure shows up in `results[i]`
with `success: false` and the rest of the batch keeps running.
This matches the typical fan-out mental model: the failed
fetches are reported alongside the successful ones, the agent
decides what to do with the partial result.

To **abort siblings on first failure**, fall back to spawning a
sub-agent ([Tutorial 4](04-multi-agent.md)) - sub-agents have
isolated lifecycles and can be cancelled cleanly. `run_parallel`
is for "best-effort batch", sub-agent spawn is for "ordered
operation with cleanup semantics".

## Comparison with the other parallelism primitives

| Primitive             | Concurrency | Lifecycle           | Best for                                              |
|-----------------------|-------------|---------------------|-------------------------------------------------------|
| **`run_parallel`**    | True parallel via `asyncio.gather` | One turn, await all | Batch fan-out of N tool calls, no per-call agent context |
| **`background_run`**  | True parallel; agent gets task_id | Survives turns, cancellable | Long-running tools the agent should poll, not block on |
| **`Agent` (spawn)**   | True parallel; full agent loop | Independent context, isolated brain | Specialists with different prompts / brains            |
| **Sequential calls**  | Serial            | One turn, await each | When call B depends on call A's result                |

The first three all use `asyncio.gather` internally; the
difference is what each call **carries**. `run_parallel` carries
just a tool call. `background_run` carries a tool call **plus a
handle** so it can outlive the turn. `Agent` carries an entire
agent loop with its own brain config.

Pick the smallest primitive that does the job. `run_parallel` is
the smallest and the cheapest.

## Going further

- The full primitive reference (every parameter, the
  result schema, error semantics):
  [Execution primitives](../language/04c-primitives.md).
- The companion `background_run` primitive for long-running
  fire-and-forget calls:
  [Advanced 9 - background_run](advanced-09-background-run.md).
- For a richer parallel pattern with isolated
  sub-agents:
  [Tutorial 4 - Multi-agent team](04-multi-agent.md).
