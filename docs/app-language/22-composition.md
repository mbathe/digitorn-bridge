---
id: composition
title: App Composition
sidebar_position: 22
---

# App Composition

Three ways to combine Digitorn apps. Each serves a different
shape of workflow.

| Pattern | Mechanism | When to use |
|---------|-----------|-------------|
| **`call_app` tool** | One agent calls another deployed app as a tool, in-flight. | Ad-hoc composition; agent decides at runtime which apps to invoke. |
| **`runtime.pipeline`** | Sequential chain of deployed apps; each step's output feeds the next. | Linear workflows known up-front (build → test → deploy). Use `runtime.mode: pipeline`. |
| **Multi-agent** | One app declares a coordinator + specialists in `agents:`. | Cohesive team behind one app; specialists share workspace + memory with the coordinator. |

Every behaviour and field on this page maps to real code; entries
are cited with file + line.

## Pattern 1 — `call_app` (in-agent app invocation)

`actions_meta.py:776` `call_app`. An always-available primitive.
The calling agent invokes another deployed app over the daemon's
HTTP API and gets the result back as the action response.

### Params (`CallAppParams`, `params.py:326`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `app_id` | string | yes | Deployed `app_id` to call. The target must be **deployed** on the same daemon and **`runtime.mode: one_shot`**. |
| `input` | string | yes | Input passed to the target's `runtime.input` contract. |
| `timeout` | float | no (default `120.0`) | Seconds before the call times out. |

### How it works

`actions_meta.py:776-804`. The action does:

1. `httpx.post("http://127.0.0.1:8000/api/apps/<app_id>/run",
   json={"input": input}, timeout=timeout)`.
2. Reads `data.success`. On true, returns
   `{ app_id, output, tool_calls }` (the target app's final
   content + how many tool calls it made).
3. On false, returns the error from the target.
4. On `httpx.ConnectError`, returns
   `"Daemon not reachable. Is it running?"`.
5. On `httpx.TimeoutException`, returns
   `f"App '{app_id}' timed out after {timeout}s"`.

### Constraints

- The target app must already be deployed: `digitorn app deploy
  target.yaml` first (or via the API).
- The target must be `runtime.mode: one_shot` — the conversation
  and background modes don't expose a `/run` endpoint that returns
  a single payload.
- The call goes through `localhost:8000` — works regardless of
  which port the daemon listens on for external traffic, because
  the agent runs **in the same daemon process** as the target app.
- Risk level: `medium` (declared in the `@action` decorator at
  `actions_meta.py:771`).

### Example

```yaml
agents:
  - id: orchestrator
    role: coordinator
    brain: { ... }
    system_prompt: |
      For Python files, call_app(app_id='py-analyzer', input=path).
      For TypeScript files, call_app(app_id='ts-analyzer', input=path).
      Aggregate the results and present a unified report.

tools:
  capabilities:
    grant:
      - { module: context_builder }   # call_app lives here
```

```json
// LLM-side
{"name": "call_app",
 "arguments": {"app_id": "py-analyzer",
               "input": "src/auth/validate.py",
               "timeout": 60}}
```

## Pattern 2 — `runtime.pipeline` (declarative chain)

`schema.py:2881` `PipelineStep`. When `runtime.mode: pipeline`, the
runtime executes `runtime.pipeline[]` in order, piping each step's
output into the next.

```yaml
app:
  app_id: build-test-deploy
  name: "Build + Test + Deploy"

runtime:
  mode: pipeline
  pipeline:
    - app: builder
      input: "{{input}}"
      output_as: artifact

    - app: tester
      input: "{{steps.0.output}}"        # OR {{artifact}}
      output_as: test_report

    - app: deployer
      input: "Deploy: {{artifact}}\nTest report: {{test_report}}"
      optional: true                      # don't fail the pipeline if deploy fails
```

### `PipelineStep` fields

`schema.py:2876` (`extra: forbid`).

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `app` | string | yes | Deployed `app_id` to invoke. |
| `input` | string | no (default `""`) | Input for this step. Supports `{{variables}}` including `{{input}}` (original pipeline input), `{{steps.N.output}}` (numeric index), and `{{<output_as>}}` (named alias from a previous step). |
| `output_as` | string | no (default `""`) | Name that later steps can use to reference this output. |
| `optional` | bool | no (default `false`) | If true, the pipeline continues even when this step fails. |

The pipeline's overall input is available as `{{input}}` in every
step. The aggregate result is the final step's output (or the last
non-optional successful step's output if the final step fails
optionally).

### When to use it

- **Strictly linear** workflows.
- Each step is itself a deployed Digitorn app that can be tested in
  isolation.
- Step boundaries are stable; you don't need an LLM to decide
  routing at runtime.

For non-linear orchestration (branches, parallel fan-out,
approvals), use the [`flow:` block](07-flows.md) instead — pipelines
are intentionally just a straight chain.

## Pattern 3 — Multi-agent inside one app

For tightly-coupled coordination where the specialists share
workspace, memory, and the agent loop, declare them under
`agents:` and use the `Agent(...)` tool to spawn from the
coordinator. Sub-agents inherit the **5 shared modules** (`memory`,
`web`, `lsp`, `filesystem`, `shell`) — the coordinator and its
specialists see the same files and the same memory state.

This is documented in detail in [Multi-Agent](12-multi-agent.md);
the relevant block is `agents[].delegate_to`,
`agents[].pool`, and the `agent_spawn.agent` tool with its 8
modes.

## Choosing between the three

| Need | Pick |
|------|------|
| Sub-task should run with **its own daemon-managed config** (different secrets, different sandbox, different deploy lifecycle). | `call_app` or `runtime.pipeline` (each app deploys independently). |
| Sub-task needs the **coordinator's workspace + memory + shell session**. | Multi-agent (sub-agents share the 5 modules). |
| **Strictly linear** workflow with stable step boundaries. | `runtime.pipeline`. |
| **Agent decides at runtime** which sub-app to call. | `call_app`. |
| Coordinator should **fan-out in parallel** to N specialists, each in its own context window. | Multi-agent (`pool.max_workers` + parallel `Agent(...)` in the same turn). |
| Conditional routing (`if X then app_A else app_B`), approval gates, decision nodes. | [`flow:` block](07-flows.md). |

## Cross-references

- App-config block reference for `runtime.pipeline`:
  [App Configuration → runtime](02-app-config.md#runtime--lifecycle-and-execution-policy)
- Built-in tool `call_app`:
  [Built-in Tools → always-available primitives](04b-builtin-tools.md#always-available-primitives-context_builder)
- Multi-agent + `Agent` tool:
  [Multi-Agent](12-multi-agent.md)
- Declarative orchestration graph (richer than pipeline):
  [Flows](07-flows.md)
- Future "App-as-MCP-Server" composition (planned):
  [App-as-MCP-Server](16-app-as-mcp-server.md)
