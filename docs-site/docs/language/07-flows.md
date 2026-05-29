---
id: flows
---

# Flows

A **flow** is a declarative orchestration graph for an app: nodes
(agents, tools, decisions, gates, ...) chained by conditional edges.
When a `flow:` block is present at the top level of the YAML, the
runtime drives the app along the explicit graph instead of relying
on the agents' system prompts to coordinate themselves via
`Agent` tool calls.

The schema is fully implemented and enforced at compile time
(strict Pydantic models, `extra: forbid` on every
node type). Cross-references (node ids, agent ids, tool names,
reachability, cycles) are validated by
`validate_flow_references`.

## Why flows?

Two coordination paradigms exist in Digitorn:

- **Implicit** - the coordinator agent decides who to call next via
  the `Agent` tool (`agent_spawn` module). Flexible but every
  decision costs an LLM round-trip.
- **Explicit (flow)** - the graph IS the orchestration. Agents are
  invoked by the flow engine; routing is decided by literal
  expressions or human approval, not by an LLM call. Deterministic,
  cheap, easier to audit.

Use a flow when:
- You can describe the workflow as a graph (triage → specialist →
  approval → output).
- The routing rules are deterministic (regex on user input,
  simple boolean expressions).
- You want a human gate at a specific point.
- You want fan-out / fan-in with a known join policy.

Stick to the implicit pattern when:
- The workflow is genuinely free-form (open-ended chat).
- The coordinator needs to make judgment calls about who's the
  right specialist for a given message.

## YAML structure

`flow:` is a **top-level block** in v2 (,
`AppDefinition.flow`). Promoted out of `runtime` to put the
two coordination models side-by-side at the top of the file.
Legacy `runtime.flow` is still accepted by the alias pass.

```yaml
flow:
  id: support_main                # required, unique within the app
  entry: triage                   # required, must be a declared node id
  description: "Support triage with refund gate"
  max_iterations: 100             # 0 = no cap (acyclic only); REQUIRED ≥ 1 if any cycle
  nodes:
    - id: triage
      type: agent
      agent: triage_bot
      input:
        user_message: "{{event.payload.message}}"
      routes:
        - { when: "category == 'refund'", to: refund }
        - { when: "category == 'tech'",   to: tech_support }
        - { when: "default",              to: end }
      on_error:
        - { match: "TimeoutError", to: triage_retry }
        - { default: true,         to: end }

    - id: refund
      type: agent
      agent: refund_specialist
      routes:
        - { to: gate }

    - id: gate
      type: approval
      message: "Confirm refund request?"
      choices: [approve, reject]
      routes:
        - { when: "approvals.gate == 'approve'", to: send_refund }
        - { when: "default",                     to: end }

    - id: send_refund
      type: tool
      tool: channels.send_message
      params:
        channel: email
        to: "{{event.payload.from}}"
        body: "Refund approved..."
      routes:
        - { to: end }

    - id: tech_support
      type: agent
      agent: tech_specialist
      routes:
        - { to: end }
```

## `FlowConfig` fields

 The top-level `flow:` block.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string (min 1) | yes | Flow identifier, unique within the app. |
| `entry` | string (min 1) | yes | Starting node id. Must be a declared node. |
| `description` | string | no | Free-form summary. |
| `max_iterations` | int ≥ 0 | conditional | Per-flow cap on total node visits. `0` = no cap (only valid for acyclic flows). **Required ≥ 1 when the graph has any cycle** to prevent infinite loops at runtime. |
| `nodes` | list[FlowNode] (min 1) | yes | Nodes that compose the graph. |

The compiler runs `validate_flow_references`
() after schema validation. It checks:

- Every `routes[].to` and `on_error[].to` references either a
  declared node or the literal sentinel `"end"` (`_END_SENTINEL`,
 ).
- Every `AgentNode.agent` references a declared `agents[].id`.
- Every `ToolNode.tool` is a real `module.action` FQN reachable
  from the declared modules.
- Every node is reachable from `entry` (no orphans).
- If any cycle is reachable, `max_iterations` is ≥ 1.

## Node types

Six discriminated variants. Each declares
`extra: forbid` and inherits four common fields from `_BaseNode`:

| Common field | Source | Description |
|--------------|--------|-------------|
| `id` | | Unique within the flow. |
| `description` | | Surfaced as the canvas tooltip. |
| `routes` | | Outgoing edges, evaluated top-to-bottom. |
| `on_error` | | Error-handling edges. The catch-all (`default: true`) must be last (validated by `_check_default_on_error_last`,). |

### `agent` - run a declared agent

`AgentNode`.

| Field | Required | Description |
|-------|----------|-------------|
| `type: agent` | yes | Discriminator. |
| `agent` | yes | `agents[].id` to execute. |
| `input` | no | Static or templated input (default `{}`). |

The agent runs for exactly one turn (system prompt + user input →
response). The response is added to the flow context under
`<node_id>.output` so downstream routes can read it.

### `tool` - direct tool invocation, no LLM

`ToolNode`.

| Field | Required | Description |
|-------|----------|-------------|
| `type: tool` | yes | Discriminator. |
| `tool` | yes | FQN, e.g. `web.search` or `http.post`. |
| `params` | no | Parameters; supports `{{templates}}`. |

The tool's response lands in the flow context under
`<node_id>.result`.

### `parallel` - fan-out, join, continue

`ParallelNode`.

| Field | Required | Description |
|-------|----------|-------------|
| `type: parallel` | yes | Discriminator. |
| `branches` | yes | List of `FlowRoute` (≥ 2 entries). Each `to` is the head of a concurrent branch. |
| `join` | no | `FlowJoin` policy (default = wait for all). |

`FlowJoin` :

| Field | Default | Description |
|-------|---------|-------------|
| `type` | `"all"` | One of `all` (wait for every branch), `any` / `first` (continue on first complete), `count` (wait for exactly `count` branches). |
| `count` | `0` | Required ≥ 1 when `type=count`. Validated by `_check_count_for_count_type`. |
| `timeout` | `60.0` (seconds, > 0) | Wall-clock cap. Branches still running when it elapses are cancelled and treated as failed. |

```yaml
- id: gather
  type: parallel
  branches:
    - { to: search_web }
    - { to: search_db }
    - { to: search_docs }
  join:
    type: count           # continue when 2 of 3 branches return
    count: 2
    timeout: 15.0
  routes:
    - { to: synthesize }
```

### `approval` - human-in-the-loop gate

`ApprovalNode`.

| Field | Required | Description |
|-------|----------|-------------|
| `type: approval` | yes | Discriminator. |
| `message` | yes (min 1) | Question shown to the user. |
| `choices` | no (default `["approve", "reject"]`, min 2) | Selectable answers. |

The user's pick is recorded as
`approvals.<node_id> = <choice>` in the flow context. Downstream
routes branch on it:

```yaml
- id: gate
  type: approval
  message: "Approve refund of $1200?"
  choices: [approve, reject, escalate]
  routes:
    - { when: "approvals.gate == 'approve'",  to: send_refund }
    - { when: "approvals.gate == 'escalate'", to: human_review }
    - { when: "default",                      to: end }
```

The flow pauses on this node - the runtime broadcasts the approval
request via the `ApprovalQueue`, and resumes when the user picks
one of the choices.

### `decision` - pure routing, no LLM, no tool

`DecisionNode`.

| Field | Required | Description |
|-------|----------|-------------|
| `type: decision` | yes | Discriminator. |
| `expr` | yes (min 1) | Expression evaluated against the flow context; routes match on the result. |

```yaml
- id: route_by_priority
  type: decision
  expr: "ticket.priority"
  routes:
    - { when: "p0", to: emergency_responder }
    - { when: "p1", to: senior_specialist }
    - { when: "default", to: standard_queue }
```

### `terminal` - end of a flow path

`TerminalNode`.

| Field | Required | Description |
|-------|----------|-------------|
| `type: terminal` | yes | Discriminator. |
| `output` | no | Final output payload returned by this path (default `{}`). |

Terminal nodes typically have empty `routes` (the flow stops). If
they do declare routes they continue as a sub-flow continuation
point - the runtime treats the path as ended for the caller's
purposes regardless.

The literal `"end"` sentinel is also accepted in
`routes[].to` to terminate a path without declaring an explicit
terminal node:

```yaml
- id: triage
  type: agent
  agent: triage
  routes:
    - { when: "category == 'refund'", to: refund }
    - { when: "default",              to: end }
```

## Routes and edges

### `FlowRoute`

 The standard outgoing edge.

| Field | Default | Description |
|-------|---------|-------------|
| `when` | `"default"` | Condition expression or the sentinel `"default"`. First match wins. |
| `to` | *required* | Target node id, or `"end"`. |

Routes are evaluated top-to-bottom in declaration order. The
expression syntax is intentionally NOT validated at the schema
layer - it's validated by the runtime expression engine when the
flow runs.

### `FlowOnErrorRoute`

 Error-handling edge.

| Field | Default | Description |
|-------|---------|-------------|
| `match` | `null` | Regex matched against the runtime error type or message. |
| `default` | `false` | Catch-all clause. Must come last when present (validated). |
| `to` | *required* | Target node when this clause matches. |

```yaml
- id: call_api
  type: tool
  tool: http.get
  params: { url: "..." }
  on_error:
    - { match: "TimeoutError",        to: retry_with_backoff }
    - { match: "AuthenticationError", to: refresh_credentials }
    - { default: true,                to: error_log }
  routes:
    - { to: parse_response }
```

## Reachability and cycles

 The compiler walks the graph from `entry` and
verifies:

- Every declared node is reachable from `entry`. Orphan nodes raise
  a compile error pointing at the unreachable id.
- Every `to` target exists (declared node or `"end"`).
- If any cycle is reachable from `entry`,
  `flow.max_iterations` must be ≥ 1. Acyclic graphs may keep
  `max_iterations: 0` (no cap).

The runtime enforces `max_iterations` per session - when the visit
counter hits the cap, the current path is forced to `"end"` and an
event is logged.

## Flow context - what nodes can read

Every node sees a small dict-like context populated as the flow
progresses:

| Path |
|------|
| `event.*` |
| `<node_id>.output` / `.result` |
| `approvals.<node_id>` |

Every `routes[].when` expression and every node's `input` /
`params` template can reference these via `{{...}}`.

## Compile-time guarantees

The validation pass (`validate_flow_references`,)
catches every common authoring mistake before deploy:

- Unknown agent reference → `unknown agent 'foo' on flow node
  'bar'`.
- Unknown tool FQN → `unknown tool 'module.bad' on flow node 'X'`.
- Dangling `routes[].to` / `on_error[].to` → `unknown target 'Y'
  on flow node 'X'`.
- Unreachable node → `flow node 'orphan' is not reachable from
  entry 'triage'`.
- Cyclic flow without cap → `flow has cycles but max_iterations=0`.
- Default error route not last → caught by
  `_check_default_on_error_last`.
- `parallel.branches` < 2 → caught by `min_length=2` on the field

- `join.type='count'` without `count >= 1` → caught by
  `_check_count_for_count_type`.

## Cross-references

- Schema definition (line-by-line):
  
- App-config block reference:
  [App Configuration → flow:](02-app-config.md#flow---declarative-orchestration-graph-8th-block)
- Multi-agent without flow (implicit coordination):
  [Multi-Agent](12-multi-agent.md)
- Tool hooks fired around node execution:
  [Tool Hooks](31-tool-hooks.md)
- Capabilities applied to flow nodes:
  [Security](11-security.md)
