---
id: tutorial-02-memory
title: "2. Conversation with memory"
sidebar_label: "2. Memory"
---

In step 1 you deployed a stateless echo bot. In this step you give
the agent **working memory**: it can remember things you tell it
and recall them later in the same conversation.

The change is small: add the `memory` module under `tools.modules`
and grant the memory actions. The agent automatically gains a set
of memory tools (`Remember`, `Recall`, `SetGoal`, `TodoAdd`, …)
that it calls when it judges something is worth keeping.

## Prerequisites

Same as step 1: a running daemon, the `digitorn` CLI installed, and
an authenticated user. This page assumes a DeepSeek credential
named `deepseek_main` is provisioned for your user; swap the
`brain` block if you use a different provider.

## The YAML

Save this as `memory-bot.yaml`:

```yaml
app:
  app_id: memory-bot
  name: Memory Bot
  version: "1.0"

runtime:
  mode: conversation
  max_turns: 10
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
      temperature: 0.3
      max_tokens: 256
    system_prompt: |
      You are a helpful assistant. When the user tells you something
      worth keeping (a name, a preference, an ongoing task), call the
      Remember tool with a short fact. When you need to look it up
      later in the conversation, call Recall.

tools:
  modules:
    memory: {}
  capabilities:
    default_policy: auto

ui:
  greeting: "Hi! Tell me anything; I'll remember the bits that matter."
```

## Deploy and chat

```bash
digitorn dev deploy memory-bot.yaml
digitorn dev chat memory-bot
```

## Live transcript

Two real turns against a running daemon, with the verbatim
output captured by the live test client. The model is the
DeepSeek `deepseek-chat` configured above.

### Turn 1

```text
> My name is Paul and I prefer terse replies.
Noted.
```

The agent fired one tool call before answering:

```text
Remember(content="User's name is Paul. Prefers terse replies.")
```

### Turn 2

```text
> What's my name?
Paul.
```

No tool call this turn: the fact was still in the agent's window
from turn 1, so it answered directly. In a longer conversation,
once that exchange has been compacted out of the live context, the
agent would call `Recall` to fetch the same fact.

The two-line replies aren't styled by the doc; the system prompt
asked for terse, and the model obliged. To replay the same trace:

```bash
digitorn dev chat memory-bot
> My name is Paul and I prefer terse replies.
> What's my name?
```

## What changed vs step 1

Two lines under `tools`:

```yaml
tools:
  modules:
    memory: {}                # adds the memory tools
  capabilities:
    default_policy: auto      # grants them automatically
```

That's it. The agent now sees `Remember`, `Recall`, `SetGoal`,
`TodoAdd`, `Forget` in its tool list and calls them when relevant.

## Going further

- **Persist across sessions**: the default `memory` module is
  in-process per session. To persist, point it at a database; see
  the [memory module reference](../reference/modules/memory.md).
- **Semantic memory**: enable `semantic: true` on the module
  config to get vector-based recall instead of plain text matching.
- **Working memory + goals**: tell the agent its overall goal at
  session start and it will use `SetGoal` and `TodoAdd` to keep
  track. See the [memory module reference](../reference/modules/memory.md).

Next: [3. Add a tool](../language/01-getting-started.md) — give
the agent the ability to read your files.
