<div align="center">

# Digitorn

**Build AI agent apps in YAML. Run them on a self-hosted runtime.**

[![License](https://img.shields.io/github/license/digitorn/digitorn-bridge?color=A78BFA)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-22D3EE)](https://www.python.org/)
[![Discord](https://img.shields.io/badge/discord-join-7289da)](https://digitorn.ai)
[![Docs](https://img.shields.io/badge/docs-digitorn.ai-34D399)](https://digitorn.ai/learn)

[Website](https://digitorn.ai) ·
[Builder](https://digitorn.ai/builder) ·
[Hub](https://digitorn.ai/hub) ·
[Templates](https://digitorn.ai/templates) ·
[Patterns](https://digitorn.ai/patterns) ·
[Migrate](https://digitorn.ai/migrate-from/langchain)

</div>

---

An entire AI agent app, including model, tools, hooks, channels, and triggers, written as one YAML file. The runtime is a Python daemon you run on your own machine. No SaaS lock-in, no JSON-Schema-by-hand, no glue code.

```yaml
# app.yaml — a Slack helper that searches the web
modules:
  channels:
    config:
      slack: { bot_token: { credential: slack_bot } }
  web: {}

agents:
  - id: helper
    modules: [{web: [search, fetch]}, {channels: [slack_post]}]
    brain:
      model: claude-haiku-4-5
      credential: anthropic_main
    system_prompt: "Answer concisely. Cite sources. Stay in the thread."
```

```bash
digitorn deploy slack-helper
# done — the agent answers @mentions, runs tools, streams responses
```

That is the whole app. No FastAPI, no LangChain, no Slack-Bolt boilerplate.

---

## Why Digitorn

| | LangChain / CrewAI | OpenAI Assistants | **Digitorn** |
|---|---|---|---|
| Source format | Python code | API + JSON Schema | **YAML** |
| Self-hosted | yes | no | **yes, open source** |
| Multi-agent | extra libs | none | **built-in dispatch** |
| Triggers / channels | bring your own | none | **Slack, webhook, cron, MCP** |
| Credentials vault | external | hosted only | **encrypted, 4 scopes, audited** |
| Visual editor | LangGraph Studio (read) | none | **bidirectional canvas** |
| Hot ceiling on cost | manual | manual | **`max_turns`, `max_tokens_per_run`** |

## What is in the box

- **Runtime daemon** with REST + Server-Sent Events streaming
- **40+ modules**: filesystem, shell, web, http, lsp, rag (qdrant), memory, image_store, sandbox, channels (slack/email/webhook), behavior engine, hooks v2 (15 events, 14 conditions, 13 actions), agent_spawn (multi-agent with parallel + abort)
- **Credentials vault**: 16-provider catalog, 4 scopes (`system_wide`, `per_app_shared`, `per_user`, `per_app_per_user`), encrypted with envelope encryption, hash-chained audit trail
- **Builder app**: conversational + visual editor with two-way YAML/canvas binding, 5 view lenses, 9-lane lifecycle map, story mode, ripple rename, 200-step undo
- **Hub**: package registry with hybrid search (Postgres + pgvector), browse and install in one command
- **TUI** for the terminal, **REST API** for everything else, native **MCP** support both directions
- **Provider list**: Anthropic (Claude Code OAuth supported), OpenAI, DeepSeek, Azure OpenAI, Mistral, Ollama, vLLM, Groq, Gemini, Together

## Install

```bash
curl -sSL https://digitorn.ai/install | sh
```

Or from source:

```bash
git clone https://github.com/digitorn/digitorn-bridge.git
cd digitorn-bridge
pip install -e ".[dev]"
digitorn daemon
```

Open `http://localhost:8000` and the Builder is the first thing you see.

## Show me one more YAML

A research crew, three specialists in parallel, one coordinator that writes the report:

```yaml
modules:
  web: {}
  agent_spawn: {}

agents:
  - id: lead
    modules: [{agent_spawn: [Agent]}]
    brain: { model: claude-sonnet-4-6, credential: anthropic_main }
    system_prompt: |
      Dispatch THREE explorers in parallel (news, academic, vendor angles).
      Wait on all three with Agent(agent_ids=[...]). Then write the report.

  - id: explorer
    role: specialist
    modules: [{web: [search, fetch]}]
    brain: { model: claude-haiku-4-5, credential: anthropic_main }
    system_prompt: "Find sources. Return facts with citations."
```

Three concurrent sub-agents, one wait, one synthesis. No LangGraph, no CrewAI Process enum, no asyncio plumbing. The pattern is documented at [digitorn.ai/patterns/fan-out-join](https://digitorn.ai/patterns/fan-out-join).

## How it compares

- **vs LangChain**: see [migrate-from/langchain](https://digitorn.ai/migrate-from/langchain). Same agent, ~85% less code, no AgentExecutor / OutputParser / Runnable wiring.
- **vs CrewAI**: see [migrate-from/crewai](https://digitorn.ai/migrate-from/crewai). Same multi-agent shape, no Crew/Task/Process classes.
- **vs OpenAI Assistants**: see [migrate-from/openai-assistants](https://digitorn.ai/migrate-from/openai-assistants). No thread polling, no requires_action, switch to DeepSeek with a one-line change.
- **vs Aider**: see [migrate-from/aider](https://digitorn.ai/migrate-from/aider). Same coding loop, served from a daemon so it lives in Slack, on PRs, on cron, anywhere.

## Production patterns, all documented

[Retry with backoff](https://digitorn.ai/patterns/retry-with-backoff) ·
[Circuit breaker](https://digitorn.ai/patterns/circuit-breaker) ·
[Fan out, join](https://digitorn.ai/patterns/fan-out-join) ·
[Semantic router](https://digitorn.ai/patterns/semantic-router) ·
[Human in the loop](https://digitorn.ai/patterns/human-in-the-loop) ·
[Rate limit with fallback](https://digitorn.ai/patterns/rate-limit-with-fallback) ·
[Summarise and feed back](https://digitorn.ai/patterns/summarize-and-feed-back) ·
[Audit everything](https://digitorn.ai/patterns/audit-everything)

## Project structure

```text
packages/digitorn/
  core/                    # Compiler, runtime, API, CLI, daemon
    app/                   # YAML schema + compiler + session manager
    runtime/               # Agent loop, hooks, behavior, agent_spawn
    credentials/           # Vault, 4 scopes, audit trail, KMS adapters
    api/                   # REST + SSE routes
    cli/                   # TUI app + dev CLI
  modules/                 # 40+ pluggable modules
    filesystem/  shell/  web/  rag/  memory/  http/
    channels/    behavior/ agent_spawn/ image_store/  sandbox/
  builtins/                # First-party apps (Builder, DeepResearch, Code, ...)
    digitorn-builder/      # The conversational + visual app builder
docs/                      # Architecture docs
examples/                  # Reference apps (incl. claude-code clone)
tests/                     # 1000+ tests
```

## Status

- **License**: Apache 2.0
- **Status**: beta, used in production by early adopters
- **Python**: 3.12+
- **OS**: macOS, Linux, Windows (Git Bash)

## Contribute

See [CONTRIBUTING.md](CONTRIBUTING.md). PRs welcome on modules, behavior rules, hooks, and migration guides.

## Star history

If Digitorn looks useful to you, a star helps other developers find it.

[![Star History Chart](https://api.star-history.com/svg?repos=digitorn/digitorn-bridge&type=Date)](https://star-history.com/#digitorn/digitorn-bridge&Date)

---

<div align="center">

**[digitorn.ai](https://digitorn.ai)** · Built by [@mbathe](https://github.com/mbathe)

</div>
