# Show HN draft — Digitorn

## Tactics that work on HN in 2026

- **Title under 80 chars**, leads with the thing, not the brand.
- **Post on Tuesday or Wednesday, 8-10 AM ET**. Strongest window for tech tools.
- **First comment within 60 seconds**: a self-comment from the author with origin story, 2 paragraphs max. HN ranks with engagement velocity.
- **Engage every reply for the first 4 hours**. Answer technical questions in detail. Answer "but what about X" honestly, including weaknesses.
- **No emojis in title or body**. HN parses them as marketing.
- **YAML/code in the body builds trust**. HN audience reads code blocks before prose.
- **Acknowledge the obvious comparison**. LangChain. CrewAI. Don't pretend they don't exist.

## Title options (pick one)

1. **Show HN: Digitorn — build AI agents in YAML, run them on a self-hosted daemon**
2. **Show HN: A Python runtime that turns YAML into multi-agent apps with channels, hooks, credentials**
3. **Show HN: I rebuilt LangChain as YAML files because the Python plumbing was killing me**

Recommendation: option 1 (clearest, no clickbait, surfaces the differentiator). Option 3 has higher CTR but invites pile-on. Option 2 is descriptive but long.

## Body (option 1 title)

```
Hey HN,

I've been building agent apps for the last year and kept hitting the same
wall. Every framework I tried (LangChain, CrewAI, Auto-GPT) made the
"hello world" easy and everything after that hard. The moment I needed
a Slack trigger, a cron, multi-tenant credentials, or just a clean
deploy story, I was writing more glue code than agent.

So I rebuilt the idea around a single YAML file plus a Python runtime
that knows how to run it.

A full Slack helper looks like this:

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

`digitorn deploy slack-helper` and the agent answers @mentions. No
FastAPI, no slack-bolt, no LangChain wrappers.

What's actually in the runtime:

- 40+ modules (filesystem, shell, web, http, lsp, rag, memory, channels,
  agent_spawn for multi-agent, behavior engine, hooks v2 with 15 events
  / 14 conditions / 13 actions)
- A credentials vault with 4 scopes (system / per-app / per-user /
  per-app-per-user), envelope encryption, hash-chained audit trail
- A Builder app that interviews you and generates the YAML, with a
  bidirectional canvas (edit YAML, canvas updates; drag a node, YAML
  rewrites). 5 view lenses (architecture / security / performance /
  runtime / sequence) on the same graph.
- Multi-agent dispatch via a single `Agent(...)` tool, parallel by
  default, with abort that actually kills child shell processes
- Sub-3s session start, SSE streaming, MCP support both directions
  (consume MCP servers, expose itself as MCP)

Honest comparison:

- **LangChain**: Digitorn collapses chains/runnables/parsers into one
  YAML file. ~85% less code on apps I've ported. LangChain still wins
  for Python-native data preprocessing in notebooks.
- **CrewAI**: same multi-agent shape, no Crew/Task/Process classes.
  CrewAI's hierarchical Process is opinionated which is a feature for
  some teams.
- **OpenAI Assistants**: same idea, self-hosted, you own the vector
  store, switch providers with one line. Assistants wins on hosted
  infra (no daemon to deploy).

What I'd genuinely like feedback on:

- Is YAML the right format? I considered TOML and a Python DSL, landed
  on YAML for readability + diff-friendliness, but happy to be told
  I'm wrong.
- The behavior engine (runtime rules that gate or rewrite tool calls
  before they execute) is the part I'm least sure I got right. It
  feels useful but adds complexity. Curious if the pattern resonates
  or feels like overengineering.
- Multi-agent abstraction: is one `Agent(...)` tool with 8 modes
  (background, blocking, batch wait, cancel, reassign, etc.) cleaner
  than separate primitives? I'm torn.

License: Apache 2.0. Python 3.12+. Works on macOS/Linux/Windows
(Git Bash). Single command install.

Repo: https://github.com/digitorn/digitorn-bridge
Docs: https://digitorn.ai/learn
Builder demo: https://digitorn.ai/builder

Roast me on what's missing, what's over-engineered, what's the wrong
abstraction. That's the kind of feedback I need.
```

Word count: ~430. HN sweet spot is 200-500.

## First self-comment (post within 60s of submission)

```
Author here. Quick context on why I built this:

I shipped a Claude-Code-style coding agent at work and the YAML file
ended up being more honest about what the agent actually did than the
Python harness around it. Stripped the harness, made the YAML the source
of truth, and the rest of Digitorn fell out of trying to make that
practical for non-trivial apps (multi-agent, scheduled, credential-aware).

The piece I'm proudest of is the credentials vault. Every YAML I'd seen
elsewhere had `api_key: "{{env.OPENAI_KEY}}"` sprinkled through it,
which works for one developer and falls apart for any multi-tenant app.
Digitorn's vault has 4 scopes resolved at deploy or session time, an
audit trail, and a TOML catalog of 16 providers so adding a new one is
a config drop, not a code change.

Happy to go deep on any of the modules in the comments.
```

## Reply templates for predictable comments

**"Why YAML and not Python/TOML/JSON?"**
> YAML hits the sweet spot for diff-friendliness, comments, and
> multi-line strings (system prompts get messy in TOML). I considered
> a Python DSL, but the moment you have one you're back to "import
> framework, configure, deploy" which is what I was trying to escape.
> JSON has no comments. The other tradeoff: YAML's whitespace
> sensitivity bites first-time users. The Builder generates valid
> YAML so most users never write it by hand.

**"How is this different from LangGraph?"**
> LangGraph is a Python state-machine library. Digitorn is a runtime
> daemon with a YAML config. Different abstraction level. You could
> port a LangGraph DAG to Digitorn but it would be one of many shapes
> the YAML supports, not the central one. LangGraph Studio is
> read-only; Digitorn's canvas writes back to the YAML.

**"What about LangChain?"**
> Honest answer: LangChain ships in 2 days, scales out at month 3
> when you need triggers, channels, multi-tenancy, deploy. Digitorn
> takes longer in week 1 (no Python tools to import) and pays back
> in month 3. There's a side-by-side migration guide:
> https://digitorn.ai/migrate-from/langchain

**"Lock-in?"**
> Apache 2.0, runs on your machine, your LLM keys never leave it.
> The YAML files are portable: nothing in the schema is Digitorn-
> specific in spirit, you could write a different runtime that
> reads them.

**"Show me a real production app"**
> The Builder itself is a Digitorn app (eat-your-own-dogfood). YAML
> is in the repo at packages/digitorn/builtins/digitorn-builder/app.yaml.
> A Claude-Code clone is in examples/opencode/app.yaml.

## Don'ts

- Don't link the same domain twice (HN flags it).
- Don't use bullet emojis (👉, 🚀).
- Don't say "production-ready" — say "beta, used in production by early
  adopters" if true.
- Don't argue with detractors. Acknowledge, ask for specifics, fix what's
  fixable, ignore trolls.
- Don't repost. One Show HN per project.
- Don't ask people to upvote. HN auto-detects vote rings.

## After 24 hours

Take the top 3 questions/criticisms from the thread and:

1. Address them in a follow-up blog post on /blog.
2. Add an FAQ entry on the relevant page (/builder, /patterns, etc.).
3. If anyone wrote a thoughtful critique, email them and ask if they
   want to chat. Some of the best contributors come from HN comments.
