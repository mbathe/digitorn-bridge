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
| 4. Multi-agent                                                | Coordinator + specialists                  | The `Agent` tool, role-based delegation, isolation        |
| 5. Background mode                                            | Cron-driven monitor                        | Triggers, channels, payload schemas                       |
| 6. UI surfaces                                                | Workspace + widgets                        | `ui.workspace` (renderer), declarative widgets            |
| 7. Deploying                                                  | Production daemon                          | TLS, credentials vault, hardening                         |

Steps 1-3 are written and tested live. Steps 4-7 are sketches; the
canonical reference for each topic lives in the
[Language](../language/) section linked from the table.

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
