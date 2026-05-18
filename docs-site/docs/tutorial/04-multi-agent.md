---
id: tutorial-04-multi-agent
title: "4. Multi-agent team"
sidebar_label: "4. Multi-agent"
---

In step 3 a single agent did everything: think, call tools, reply.
In this step you split the work across **specialists** that the
**coordinator** delegates to. The coordinator is the agent the user
talks to; each specialist is a fresh agent loop spawned on demand
through the `Agent` tool.

You add one module (`agent_spawn`) and declare more than one entry
under `agents:`. The coordinator picks who to dispatch to and runs
them in parallel.

## Prerequisites

Same as the previous steps (running daemon, authenticated user,
DeepSeek credential `deepseek_main` provisioned).

## The YAML

Save this as `multi-agent.yaml`. Three agents share the same brain
config (a per-user DeepSeek credential), but each has its own role
and system prompt.

```yaml
app:
  app_id: multi-agent
  name: Multi-Agent Team
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 8
  timeout: 180

agents:
  - id: coordinator
    role: coordinator
    brain: &deepseek
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
      max_tokens: 512
    system_prompt: |
      You are a coordinator. You have two specialists you can spawn
      in parallel using the Agent tool: `summarizer` and `translator`.
      For any user question that needs both a summary and a French
      translation, spawn both specialists IN PARALLEL with
      Agent(prompt="...", specialist="summarizer", wait=true) and
      Agent(prompt="...", specialist="translator", wait=true). Then
      combine their results into a single answer. Keep the final
      answer short.

  - id: summarizer
    role: specialist
    brain: *deepseek
    system_prompt: |
      You summarise the input in ONE sentence. No preamble, no
      explanation, just the one-sentence summary.

  - id: translator
    role: specialist
    brain: *deepseek
    system_prompt: |
      You translate the user's input to French. Output only the
      translation, no preamble.

tools:
  modules:
    agent_spawn: {}
  capabilities:
    default_policy: auto

ui:
  greeting: "Coordinator + 2 specialists. Try: 'Summarise and translate this paragraph...'"
```

The YAML anchor (`&deepseek` / `*deepseek`) reuses the same brain
across the three agents to avoid copy-paste. Anything is allowed
here: a different model per specialist, a cheaper one for the
coordinator, etc. Read more on agent shapes in the
[Agents reference](../language/03-agents.md).

## Deploy and chat

```bash
digitorn dev deploy multi-agent.yaml
digitorn dev chat multi-agent
```

## Live transcript

Sample transcript. The user paragraph asks the
coordinator to summarise and translate at once.

```text
> Summarise and translate this paragraph: Solar panels in Europe
  produced more electricity than coal in 2023, marking the first
  time renewables outpaced any single fossil fuel across the
  continent.

Here are the results:

**Summary:** Solar panels in Europe generated more electricity
than coal in 2023, a historic first where renewables exceeded a
single fossil fuel continent-wide.

**French translation:** Les panneaux solaires en Europe ont
produit plus d'électricité que le charbon en 2023, marquant la
première fois que les énergies renouvelables surpassaient un
seul combustible fossile sur le continent.
```

Behind the scene the coordinator fired exactly **two tool calls**
(`tool_calls_count: 2` on the `result` event). The live event
stream shows both specialists spawned in parallel:

```text
[agent_event] specialist=summarizer status=spawned
[agent_event] specialist=summarizer status=running   duration=2.1s
[agent_event] specialist=summarizer status=completed
              preview="Solar panels in Europe generated more
                       electricity than coal in 2023, a historic
                       first where renewables exceeded a single
                       fossil fuel continent-wide."

[agent_event] specialist=translator status=spawned
[agent_event] specialist=translator status=running   duration=2.1s
[agent_event] specialist=translator status=completed
              preview="Les panneaux solaires en Europe ont produit
                       plus d'électricité que le charbon en 2023,
                       marquant la première fois que les énergies
                       renouvelables surpassaient un seul
                       combustible fossile sur le continent."
```

Both specialists finished in roughly two seconds because they ran
concurrently through `asyncio.gather`. The coordinator received
both results back in the same turn and produced the combined reply.

## What changed vs step 3

Two things. First, `agents:` now lists three entries with distinct
`role` and `system_prompt` blocks. The coordinator's role is
`coordinator`; the workers' role is `specialist`. The coordinator
is the `entry_agent` by default - it's the one the user talks to.

Second, `tools.modules.agent_spawn: {}` is loaded. That module
exposes the `Agent` tool to the coordinator. The eight modes of
the `Agent` tool (background, blocking, status, wait, cancel,
reassign, list) are documented in the
[agent_spawn module reference](../reference/modules/agent_spawn.md).

```yaml
tools:
  modules:
    agent_spawn: {}             # adds the Agent tool
  capabilities:
    default_policy: auto
```

## When to use this pattern

A flat single-agent setup is enough when the work is sequential.
Multi-agent shines when you can fan out:

- **Parallel reads** - search across N data sources, merge results
- **Specialised tools** - one agent owns the database tools, another
  owns the file system, another owns web search
- **Isolation** - spawn a writer agent with `default_policy: block`
  on filesystem so it can't touch disk while the coordinator can

The coordinator stays in charge of the conversation. Specialists
return raw output and exit. They don't carry their own user
session - their context dies with the spawn.

## Going further

- The `Agent` tool has eight modes (`wait=true`, `agent_id=...`,
  `cancel=true`, `reassign=...`, `list=true`, parallel `Agent()`
  calls in one turn). See the
  [agent_spawn reference](../reference/modules/agent_spawn.md).
- Granular per-specialist tool restriction:
  `agents[].modules: [{filesystem: [read]}]`. The specialist sees
  only the listed actions, not the coordinator's full toolbox.
  See [Multi-agent](../language/12-multi-agent.md).
- Shared modules across spawn: `memory`, `web`, `filesystem`,
  `shell` are shared by default; everything else gets a fresh
  instance per specialist. The same `WORKSPACE` dir is shared so
  files written by one specialist are visible to the next.

Next: explore the [Agent tool reference](../reference/modules/agent_spawn.md)
or pick a [module from the index](../reference/modules/) for your
next experiment.
