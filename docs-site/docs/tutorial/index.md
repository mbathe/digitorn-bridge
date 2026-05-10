---
id: tutorial-index
title: Tutorial
---

A linear path from "hello world" to a production-shape multi-agent
app. Read in order. Each step builds on the previous one and ends
with a verified live test.

## Prerequisites

- Python 3.12+
- A running daemon (`digitorn start`)
- Either a local Ollama instance OR an API key for one of the
  supported providers
  ([Validated provider hints](../language/03-agents.md#validated-provider-hints))

## Steps

| Step                                                          | What you build                             | What you learn                                            |
|---------------------------------------------------------------|--------------------------------------------|-----------------------------------------------------------|
| [1. Getting started](../language/01-getting-started.md)       | Hello-world chatbot                        | Install, validate, deploy, chat from the CLI              |
| [2. Conversation with memory](02-conversation-with-memory.md) | Assistant that remembers facts you tell it | System prompts, the memory module, `Remember` / `Recall`  |
| [3. Add a tool](03-add-a-tool.md)                             | Bot that reads files in your workspace     | Modules, capabilities, the filesystem module              |
| [4. Multi-agent team](04-multi-agent.md)                      | Coordinator + 2 parallel specialists       | The `Agent` tool, role-based delegation, isolation        |
| [5. Background mode](05-background-mode.md)                   | Cron-driven monitor                        | Triggers, channels, payload schemas                       |
| [6. UI surfaces](06-ui-surfaces.md)                           | Workspace pane the agent writes into       | `ui.workspace` (renderer), workspace module               |
| [7. Deploying](07-deploying.md)                               | Production-shape app with deny-by-default  | Capabilities, behaviour profile, credential schema        |

Every step is written and tested live: each YAML deploys against
the daemon, every transcript shown is the verbatim output, every
tool call count and trigger result was captured by the live event
stream.

## What you'll have at the end

A multi-agent app that:

- Spawns specialist sub-agents in parallel
- Persists working memory across conversations
- Has a workspace pane the agent writes to in real time
- Connects to one external service via the credentials vault
- Has a background trigger that wakes it on a cron schedule
- Runs sandboxed in production

## When to leave the tutorial

The tutorial is opinionated and linear. Once you've finished it,
work from [Reference](../reference/) and [Language](../language/)
directly - those are the canonical surfaces. Come back to
[Concepts](../concepts/) any time the framework's *why* is unclear.
