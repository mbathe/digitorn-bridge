---
id: advanced-index
title: Advanced tutorials
sidebar_label: Advanced (intro)
---

The first seven tutorials covered the basic shape of a Digitorn
app: a YAML, a brain, a few tools, capabilities, deploy, chat.
The advanced tutorials go one level deeper. Each one isolates a
single primitive, explains the reasoning behind it, and finishes
with a real live transcript captured against the daemon.

Read them in any order. They share no narrative thread; pick the
one that solves a problem you actually have.

| Tutorial                                                                       | What it teaches                                                              |
|--------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| [Advanced 1 - Sub-agent isolation](advanced-01-sub-agent-isolation.md)         | Per-agent module restriction: granular tool surface per specialist           |
| [Advanced 2 - Bundle namespaces](advanced-02-bundle-skills.md)                 | Multi-file app bundles, `{{prompt.X}}`, `{{skill.X}}`, slash commands        |
| [Advanced 3 - Discovery mode for large toolsets](advanced-03-discovery.md)     | Lazy tool-schema loading via meta-tools (`browse_category`, `get_tool`)      |
| [Advanced 4 - Behavior engine](advanced-04-behavior.md)                        | Declarative runtime rules that constrain what the agent can do, in real time |
| [Advanced 5 - Middleware pipeline](advanced-05-middleware.md)                  | LLM-call wrappers: `mask_secrets`, `content_filter`, `rag_inject`            |
| [Advanced 6 - Brain fallback](advanced-06-brain-fallback.md)                   | Failover to a backup brain on billing-class errors                           |
| [Advanced 7 - Hooks pipe (tool chaining)](advanced-07-hooks-pipe.md)           | Auto-route one tool's output into another, no LLM involvement                |
| [Advanced 8 - App composition with call_app](advanced-08-composition.md)       | One agent invoking another deployed app as a tool                            |
| [Advanced 9 - Background tool execution](advanced-09-background-run.md)        | Launch long-running shell jobs without freezing the conversation             |
| [Advanced 10 - Parallel tool execution](advanced-10-run-parallel.md)           | Fan out N independent tools concurrently, gather all results in one round    |
| [Advanced 11 - Session forking](advanced-11-session-fork.md)                   | Clone a session to branch a conversation, A/B test, or run what-if scenarios |
| [Advanced 12 - MCP server integration](advanced-12-mcp-server.md)              | Plug any Model Context Protocol server in and expose its tools to the agent  |
| [Advanced 13 - Locking down the toolset](advanced-13-locking-down-tools.md)    | Three orthogonal mechanisms to mask, deny, or parameter-block tools          |
| [Advanced 14 - Red-team report](advanced-14-redteam.md)                        | Five attacks against the live daemon - what holds, what leaks, real bugs    |

## What "advanced" means here

These pages assume you know the seven base tutorials, that you've
deployed an app and chatted with it, and that you can read a YAML
without consulting the language reference for every field.

Each one introduces a primitive that **changes the shape of an
app, not just its content**. The basic tutorials let you build a
chatbot; the advanced ones let you build an agent that survives
production.

## What each one is for

**Sub-agent isolation** is the right tool when you have one trusted
coordinator and N less-trusted specialists. The coordinator can do
anything; specialists see only the slice they need. Useful for
review pipelines, plug-in agents, and any setup where a
specialist's prompt could be hostile.

**Bundle namespaces** matter the moment your system prompt grows
past a paragraph or you want shippable slash commands. The bundle
shape is also the canonical packaging form - the marketplace
expects it.

**Discovery mode** is the answer when your app loads many
modules. Direct injection of every tool schema becomes
prohibitively expensive past ~50 tools; discovery lets the model
navigate the catalogue at runtime instead.

**Behavior engine** is what makes a hasty model methodical.
System prompts say "please do X"; behavior rules enforce X, in
real time, with hard blocks for destructive actions and soft
nudges for hygiene. Production code-editing apps usually want
both.

## Where these primitives compose

The four primitives layer cleanly:

- A **bundle** holds the YAML, a long system prompt, and slash
  commands.
- The YAML uses **discovery mode** because the toolbox is large.
- It declares a **behavior profile** so the agent reads-before-edits
  and tests-after-changes without having to be told every turn.
- It uses **sub-agent isolation** so the writer specialist sees
  filesystem.write but the auditor specialist sees only
  filesystem.read.

The full integrated shape lives in the in-product Builder and
Code apps. Read those to see what a polished version looks like.

## Going further

For the runtime knobs not covered in any tutorial:
- [Hooks v2](../language/31-tool-hooks.md) - imperative
  pre/post-tool callbacks (gate, transform_params,
  transform_result, pipe).
- [Middleware](../language/17-middleware.md) - LLM-call wrappers
  for secret masking, content filtering, RAG injection.
- [Triggers and channels](../language/40-channels.md) - 11
  bidirectional adapters for webhooks, email, Slack, Telegram,
  Discord, RSS, voice.
- [Multi-tenant installs](../language/45-multi-tenant.md) -
  per-user vs system-wide deployment.

For the architecture that makes all of this possible:
- [Concepts](../concepts/) for the why.
- [Reference](../reference/) for the granular surface.
