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

## What you build

1. A coordinator agent (role `coordinator`) emits three
   `Agent(specialist='analyst', wait=true)` tool calls.
2. The runtime instantiates three isolated analyst sub-agents,
   each with its own system prompt and isolated context.
3. Each analyst runs its own LLM turn and returns a structured
   report inline in the tool result.
4. The coordinator collects the three reports and writes a
   synthesis in the chat.

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
  Without it any spawn that omits `specialist=` falls back to
  "ad-hoc" mode and fails with
  `"No coordinator provider configured"`.
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

The prompt asks the agent explicitly to make three
`Agent(specialist='analyst', wait=true)` calls. With a larger
model the natural prompt *"compare X / Y / Z by spawning
analysts"* routes correctly without explicit hand-holding.

## Sample analyst output

Each spawn returns the analyst's full report inline in the tool
result. Example (PostgreSQL analyst):

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

## Sample coordinator synthesis

```
Overall verdict
- PostgreSQL: best when you need strong, predictable ACID
  semantics, complex relational queries, and rich SQL
  features. Suits OLTP, analytic workloads with structured
  schema and where correctness matters.
- MongoDB: best when you need a flexible document model for
  hierarchical/variable data, fast developer iteration, and
  high write/ingest throughput.
- Redis: best as an ultra-low-latency, in-memory data plane
  (cache, counters, queues, leaderboards, ephemeral state).

PostgreSQL
- Data model
  - Relational, strongly schema-based: tables, rows, typed
    columns, constraints, foreign keys, joins, stored
    procedures.
  - Rich types (arrays, JSONB, hstore, ranges, enums), advanced
    indexing (B-tree, GiST, GIN, BRIN), materialized views,
    partitioning - so you can model semi-structured data
    (JSONB) while keeping relational integrity.
[...]
```

## Parallel vs sequential execution

The runtime can run multiple sub-agent spawns in parallel
through `asyncio.gather`. When **every** tool_call in a single
assistant message is a sub-agent spawn, the loop fires them
concurrently and total time is roughly `max(individual times)`.

Whether the model emits all spawns in one message or one per
round-trip depends on its tool-calling discipline. Models that
batch multiple tool calls per assistant message parallelise the
workload; models that emit one tool per round-trip serialise it.

If you want guaranteed parallelism on a non-batching model, use
the `wait=false` + collect flow:

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
call.

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
