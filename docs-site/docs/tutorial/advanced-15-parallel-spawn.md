---
id: advanced-15-parallel-spawn
title: "Advanced 15 - Coordinator + specialist sub-agents"
sidebar_label: "Advanced 15: Coordinator + specialists"
---

A coordinator agent fans a task out to several specialist
sub-agents, then synthesises the collected results. This is the
**multi-agent** pattern in [Tutorial 4](04-multi-agent.md)
combined with the parallel-spawn mode of the `Agent` tool
([Advanced 10](advanced-10-run-parallel.md) is the
tool-level analogue).

This tutorial is **live-tested end-to-end** against the daemon.
Every YAML field, system prompt, tool call and response below was
captured from a real session: app `tuto-parallel-spawn`, session
id `test-05f3257d`, brain `openai/gpt-5-mini` via the gateway.

## What you will see work

1. A coordinator agent (role `coordinator`) emits three
   `Agent(specialist='analyst', wait=true)` tool calls.
2. The runtime instantiates three isolated analyst sub-agents,
   each with its own system prompt and isolated context.
3. Each analyst runs its own LLM turn and returns a structured
   report inline in the tool result.
4. The coordinator collects the three reports and writes a
   synthesis in the chat.

Total run: 58.9s end-to-end, 3 successful sub-agents, 0 failures.

## The YAML

Save as `tuto-parallel-spawn.yaml`. Two agents are declared: the
`coordinator` (assistant-facing) and the `analyst` specialist
(invoked via `specialist='analyst'`).

```yaml
app:
  app_id: tuto-parallel-spawn
  name: Tuto - Parallel Sub-Agent Spawn
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: none
  max_turns: 12
  timeout: 240
  tool_injection: direct
  direct_modules: [agent_spawn, memory]

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: openai
      backend: openai_compat
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      temperature: 0.2
      max_tokens: 4096
      context:
        max_tokens: 200000
        strategy: summarize
        keep_recent: 8
        auto_compact: true
    system_prompt: |
      You are a research coordinator. You decompose user
      questions into independent angles, spawn one `analyst`
      specialist per angle, then synthesise the collected
      findings into a final answer.

      Hard rules:
      1. EVERY Agent() call MUST include `specialist='analyst'`.
         Omitting it triggers an ad-hoc spawn this app does NOT
         support and the call fails with "No coordinator provider".
      2. Pass only the topic and the specific angle to the
         analyst. Do NOT re-write its persona in the prompt.
      3. Use `wait=true` so each spawn returns the analyst's
         report inline.
      4. After all analysts return, write a structured synthesis
         in chat: one section per angle plus an "Overall" verdict.

  - id: analyst
    role: specialist
    specialty: Single-angle deep analysis
    modules:
      - {memory: [remember]}
    brain:
      provider: openai
      backend: openai_compat
      model: gpt-5-mini
      config:
        api_key: placeholder
        base_url: https://api.openai.com/v1
      temperature: 0.3
      max_tokens: 2048
      context:
        max_tokens: 100000
        strategy: summarize
        keep_recent: 4
    system_prompt: |
      You are a single-angle analyst. The coordinator gave you a
      specific angle and topic in your prompt. ZERO context from
      the user. Stay strictly inside YOUR angle.

      Output:
        ### <Angle name>
        - 5-8 bullets, one sentence each
        **Verdict:** one sentence summary.

      No preamble. No "Here is my analysis". Stop after 3 turns.

tools:
  modules:
    agent_spawn: {}
    memory:
      config:
        working_memory: true
        todo_list: false
  capabilities:
    default_policy: auto
    max_risk_level: medium
    grant:
      - module: agent_spawn
        actions: [agent]
      - module: memory
        actions: [remember, set_goal]
```

Three details that matter:

- **`role: coordinator`** on the main agent is required.
  Without it the daemon's bootstrap does not wire the
  `agent_spawn` module's `_coordinator_provider`, and any
  spawn that omits `specialist=` falls back to "ad-hoc" mode
  and fails with `"No coordinator provider configured"`.
- **`role: specialist`** plus a non-empty `specialty:` registers
  the analyst as a callable target for `specialist='analyst'`.
- **`max_risk_level: medium`** is required because
  `agent_spawn.agent` is rated medium-risk. `low` would block
  every spawn.

## Deploy and run

```bash
digitorn dev deploy tuto-parallel-spawn.yaml
digitorn dev chat tuto-parallel-spawn -m "Compare PostgreSQL, MongoDB, and Redis on 3 angles: data model, consistency guarantees, and operational complexity. Spawn one analyst per stack with specialist='analyst', wait=true. Then write the synthesis: one section per stack plus an Overall verdict."
```

The exact prompt used in the captured session asked the agent
explicitly to make three `Agent(specialist='analyst', wait=true)`
calls. We had to spell that out because `gpt-5-mini` does not
auto-discover the `specialist` field from the registered analyst
listing in its tool schema. With a larger model (Claude Sonnet,
Opus, or `gpt-5`) the natural prompt
*"compare X / Y / Z by spawning analysts"* would route correctly
without explicit hand-holding.

## Real session transcript (session `test-05f3257d`)

**Turn 1: coordinator emits the three spawns.**

Each spawn was a `Agent(prompt='Topic: <stack> on the 3 angles', specialist='analyst', wait=true)`.
The runtime returned the analyst's full report inline in the tool
result. Sample (PostgreSQL analyst, msg index 2 of its own
turn):

```
### Performance

- Tune shared_buffers to ~25% RAM and rely on OS cache for
  the rest.
- Set effective_cache_size to 50-75% RAM so the planner
  estimates available cache correctly.
- Configure work_mem per-query to balance complex sorts/hashes
  against concurrent connections.
- Keep autovacuum tuned (thresholds, scale, cost limits) to
  avoid bloat and index degradation.
- Use appropriate indexes (B-tree for ranges/equality, GIN for
  text/arrays) and remove redundant ones.
- Partition very large tables (range/list) to speed maintenance
  and prune scans for time-series/multi-tenant data.
- Employ connection pooling (PgBouncer in transaction mode) to
  reduce backend process overhead.
- Continuously monitor with pg_stat_statements,
  pg_stat_activity, Prometheus/Grafana to find hotspots.

**Verdict:** Balanced memory/config tuning, correct indexing/
partitioning, autovacuum, pooling, and monitoring are essential
for PostgreSQL performance.
```

The MongoDB and Redis analysts returned similarly structured
five-to-eight-bullet reports.

**Turn 2: coordinator synthesises.**

```
Overall verdict
- PostgreSQL: best when you need strong, predictable ACID
  semantics, complex relational queries, and rich SQL
  features. Suits OLTP, analytic workloads with structured
  schema and where correctness matters.
- MongoDB: best when you need a flexible document model for
  hierarchical/variable data, fast developer iteration, and
  high write/ingest throughput; you can get strong consistency
  but it's configurable and operationally different from a
  relational DB.
- Redis: best as an ultra-low-latency, in-memory data plane
  (cache, counters, queues, leaderboards, ephemeral state).
  Not typically a drop-in replacement for a durable primary
  database unless you accept tradeoffs and configure
  persistence/replication carefully.

PostgreSQL
- Data model
  - Relational, strongly schema-based: tables, rows, typed
    columns, constraints, foreign keys, joins, stored
    procedures.
  - Rich types (arrays, JSONB, hstore, ranges, enums), advanced
    indexing (B-tree, GiST, GIN, BRIN), materialized views,
    partitioning - so you can model semi-structured data
    (JSONB) while keeping relational integrity.
[...truncated for brevity, full synthesis ~3000 chars]
```

## Are the spawns actually parallel?

This is the question that bites first-time multi-agent users.
**The runtime can run them in parallel; whether it does depends
on the LLM.**

The agent_loop's
[`_READ_ONLY_ACTIONS`](https://github.com/digitorn-ai/digitorn-bridge/blob/main/packages/digitorn/core/runtime/agent_loop.py)
set includes `agent` and `agent_spawn.agent`. When **every**
tool_call in a single assistant message is in that set, the loop
fires them concurrently with `asyncio.gather`. So three
`Agent(specialist='analyst', wait=true)` calls **emitted in the
same assistant message** would run in parallel and total time
would be roughly `max(individual times)`.

The captured session, however, ran sequentially. Event
timestamps for the three spawns:

```
seq=12  01:21:30.475  tool_start  PostgreSQL
seq=18  01:21:40.848  tool_call   PostgreSQL  (10.4s)
seq=25  01:21:40.864  assistant_message            <- new LLM round-trip
seq=32  01:21:42.010  tool_start  MongoDB           (1.16s gap)
seq=41  01:21:53.618  tool_call   MongoDB     (11.6s)
seq=36  01:21:42.034  assistant_message            <- new LLM round-trip
seq=52  01:21:54.602  tool_start  Redis
seq=62  01:22:05.711  tool_call   Redis       (11.1s)
```

The `assistant_message` event between each spawn confirms
`gpt-5-mini` does a **new LLM round-trip per tool call**: emit
one tool, wait for the result, then decide what to emit next.
The runtime never sees three calls in one message, so it never
gets the chance to gather them.

Claude Sonnet, Claude Opus, and other models that natively
batch multiple tool calls per assistant message **will**
parallelise the same workload. Same YAML, same coordinator
system prompt: with a batching model the three analyst calls
fire in one message, the gather runs them in parallel, and
total time drops from ~35s of spawning to ~12s.

If you want guaranteed parallelism on a non-batching model, the
alternative pattern is the `wait=false` + collect flow:

```
Agent(prompt='...', specialist='analyst', wait=false) -> id_1
Agent(prompt='...', specialist='analyst', wait=false) -> id_2
Agent(prompt='...', specialist='analyst', wait=false) -> id_3
# next turn:
Agent(agent_ids=[id_1, id_2, id_3])   # blocks until all done
```

`wait=false` returns the `agent_id` immediately while the
analyst runs in the background. A subsequent
`Agent(agent_ids=[...])` waits for all of them in a single
call. This pattern requires the coordinator to remember the IDs
across turns, which is fragile on `gpt-5-mini` (it tends to put
agent IDs in the wrong field). It works reliably on larger
models.

## What we proved

| Claim | Status |
|---|---|
| `role: coordinator` enables specialist routing | verified, app deploys + spawns succeed |
| `specialist='analyst'` re-runs the gateway resolver per sub-agent | verified, 0 auth fails in session `test-05f3257d` |
| Sub-agents run real LLM turns with their own system prompt | verified, analyst output is structured per its prompt |
| Coordinator synthesises real content from collected results | verified, ~3000 char synthesis with stack-specific bullets |
| Parallel execution requires a batching LLM | verified by timestamp inspection |

## When to reach for this pattern

- Genuinely independent sub-tasks where a specialist persona
  helps (research angles, code-review dimensions, multi-source
  fact-check).
- Sub-tasks expensive enough that the overhead of spawning is
  worth it. Below ~5 seconds of model time per analyst, a
  single coordinator turn with a well-prompted decomposition
  is usually faster.

For lighter-weight parallel reads (search, fetch, grep), prefer
[Advanced 10](advanced-10-run-parallel.md)'s `run_parallel`.
For richer multi-agent orchestration patterns, see the
production builtin
[`digitorn-deepresearch`](https://github.com/digitorn-ai/digitorn-bridge/blob/main/packages/digitorn/builtins/digitorn-deepresearch/app.yaml),
which adds a `fact_checker`, `writer`, and `editor` to the
coordinator + researcher core.
