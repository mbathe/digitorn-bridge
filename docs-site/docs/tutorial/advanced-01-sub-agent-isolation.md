---
id: advanced-01-isolation
title: "Advanced 1 - Sub-agent isolation"
sidebar_label: "Advanced 1: Isolation"
---

In tutorial 4 you spawned specialists in parallel. Each specialist
got the **full** module set the parent app declares. That's fine
for trusted teams of agents but wrong when you want a coordinator
who can do everything and a worker who can only read.

This tutorial uses **per-agent module restriction** to give each
specialist a different slice of the toolbox. The reader sees
`[read, glob, grep]` only; the writer sees the full filesystem.
The coordinator sees both, plus the `Agent` tool to dispatch.

## The pattern

Each entry in `agents[].modules` can be one of two shapes:

```yaml
modules:
  - filesystem                          # full module access
  - {filesystem: [read, glob, grep]}    # ONLY these three actions
  - {memory: [remember]}                # one action only
```

The simple form (a string) grants the whole module. The dict form
restricts to a named action subset. The coordinator's modules act
as the **superset** - a specialist cannot see a module the
coordinator doesn't have.

## The YAML

Save this as `isolation-bot.yaml`. Three agents share the same
brain (DeepSeek) and dispatch through `agent_spawn`.

```yaml
app:
  app_id: isolation-bot
  name: Isolation Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 8
  timeout: 120

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
      max_tokens: 400
    system_prompt: |
      You are the coordinator. You have two specialists:
      - `reader` (read-only file access)
      - `writer` (write access)
      Use Agent(prompt="...", specialist=<id>, wait=true) to dispatch.
      For tasks like "summarise this file", call reader. For tasks
      like "write a new file", call writer. Be concise.

  - id: reader
    role: specialist
    brain: *deepseek
    modules:
      - {filesystem: [read, glob, grep]}    # read-only slice
      - memory                              # full memory module
    system_prompt: |
      You read files. You CANNOT write or edit anything. If asked
      to write, reply: "I cannot write files; I only have read access."

  - id: writer
    role: specialist
    brain: *deepseek
    modules:
      - filesystem                          # full filesystem access
      - memory
    system_prompt: |
      You write files. Use Write to create them.

tools:
  modules:
    filesystem: {}
    memory: {}
    agent_spawn: {}
  capabilities:
    default_policy: auto
```

The `&deepseek` / `*deepseek` YAML anchor is just to avoid copying
the brain block three times. The interesting part is the
`agents[].modules` declaration.

## What the system actually does

When the coordinator calls `Agent(prompt="...", specialist="reader",
wait=true)`, the daemon does three things:

1. **Look up reader's `modules` list.** It computes the action filter
   `{"filesystem": {"read", "glob", "grep"}, "memory": ALL}`.
2. **Build the reader's tool index** with only the matching actions.
   The LLM running as reader sees `Read`, `Glob`, `Grep`, plus all
   memory actions. `Write`, `Edit`, `Delete` are not in the schema -
   the LLM does not know they exist.
3. **Run the agent loop.** Even if the reader's LLM hallucinates a
   "Write" call, the dispatcher rejects it at gate 1 because the
   action is not in the agent's profile.

This is not just a system-prompt instruction. It's a hard schema
restriction.

## Live transcript

Sample transcript. The coordinator gets one user
message that explicitly tries to make the **reader** write a file
(to verify the gate).

```text
> Use the reader specialist to write a file called test.txt with
  content "hi". I want to see what the reader does when asked to
  write.
```

The live event stream captured the full dispatch sequence:

```text
agent_event  specialist=reader  status=spawned
agent_event  specialist=reader  status=running
agent_event  specialist=reader  status=completed
             preview="I cannot write files; I only have read access."

agent_event  specialist=writer  status=spawned
agent_event  specialist=writer  status=running   (4 sub-turns)
agent_event  specialist=writer  status=completed
             preview="Done. Created test.txt with content 'hi'."
```

The coordinator dispatched the request to `reader` first. Reader
ran, recognised the request was outside its allowed surface, and
responded with the canned refusal from its system prompt -
**because the Write tool was not in its tool index**, the LLM
literally couldn't try to call it.

The coordinator then escalated to `writer`. Writer ran four
sub-turns (resolve workspace path, write the file, verify, reply)
and completed successfully.

The final coordinator reply combined both specialist results into
one user-facing message. Two `Agent()` tool calls fired in total
(`tool_calls_count: 2`).

## Two ways to enforce restrictions

The example above uses **per-agent module restriction**. There's
also **per-app capability restriction** (covered in
[tutorial 7](07-deploying.md)). They compose:

- `agents[].modules` filters which **actions a specific agent**
  can see, regardless of what the parent app grants.
- `tools.capabilities.grant` filters which **actions any agent**
  can call, regardless of the agent profile.

Apply capabilities first to enforce app-wide rules. Use per-agent
modules to give a tighter surface to specialists that don't need
the full toolbox.

## Module sharing across spawn

Some modules carry per-session state that must be **shared** with
sub-agents. Memory is the clearest example: the coordinator
remembers facts the user told it; specialists need to see those
facts. Workspace, shell, web, lsp, and filesystem behave the same
way.

The runtime treats those five modules as `isolation: shared` -
sub-agents inherit the same instance the coordinator uses. The
others (database, channels, rag, vector, …) get a fresh instance
per spawn so concurrent specialists do not stomp on each other.

The shared / fresh split is documented per module in the
[module reference](../reference/modules/). The agent author does
not pick this; the module declares its isolation level and the
spawn runtime obeys it.

## When this pattern earns its keep

- You ship an app with a **dangerous tool** (shell, network)
  that only one specialist should ever call. Coordinator sees
  it, the rest do not.
- You build a **review pipeline** where the writer agent runs
  first, then a separate read-only auditor agent vets the
  output. The auditor literally cannot mutate anything it
  reviews.
- A **third-party agent** plugged into your app via
  `agent_spawn` gets a deliberately sandboxed module subset.
  Even if the third party's prompt is hostile, it cannot reach
  beyond the granted actions.

For trust at scale this is more reliable than relying on system
prompts alone. The LLM never gets the chance to try - the action
is not in its world.

## Going further

- The full multi-agent reference is in
  [Multi-Agent](../language/12-multi-agent.md). It covers shared
  modules, abort cleanup, granular filtering for nested spawns,
  and the eight modes of the `Agent` tool.
- For tools the coordinator wants to **gate per request** rather
  than per agent, use the
  [behaviour engine](../language/43-behavior.md). Behaviour rules
  are evaluated at runtime, not at agent construction.
- For the daemon-level gate (deny by default for the whole app)
  see [tutorial 7](07-deploying.md) on capabilities.
