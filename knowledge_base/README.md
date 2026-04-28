# Digitorn Knowledge Base

This directory holds the **distilled, RAG-ready** knowledge that powers the
Digitorn App Builder agent (and any other Digitorn app that wants to query
"what does the framework support").

It is **separate** from `docs/` on purpose:

| `docs/` | `knowledge_base/` |
|---------|-------------------|
| Long-form, written for humans | Short, atomic, written for the LLM |
| Markdown with prose, narrative, tables | Markdown cards with strict frontmatter |
| Edited manually, hard to chunk well | Generated from `docs/`, easy to embed |
| Lives in the docs site | Lives in the RAG (`~/.digitorn/knowledge_base/.qdrant/`) |

The two stay in sync via `distill.py` - which reads `docs/*.md` and emits
atomic concept cards into `concepts/`.

## Layout

```
knowledge_base/
├── README.md                  ← you are here
├── distill.py                 ← LLM-powered distillation: docs → concept cards
├── generate_modules.py        ← introspects the daemon's @action registry → module cards
├── build.py                   ← ingests all 3 collections into the RAG module
│
├── concepts/                  ← ~80 atomic cards, one .md per concept
│   ├── trigger-cron.md
│   ├── trigger-watch.md
│   ├── payload-schema.md
│   ├── session-mode-mono-vs-multi.md
│   ├── capabilities-grant.md
│   └── ...
│
├── modules/                   ← auto-generated, one .md per @action
│   ├── filesystem-read.md
│   ├── filesystem-write.md
│   ├── web-search.md
│   └── ...
│
└── examples/                  ← starter app templates (the 5 archetypes)
    ├── 01-scheduled-monitor.yaml
    ├── 02-conversational-assistant.yaml
    ├── 03-event-webhook-processor.yaml
    ├── 04-document-pipeline.yaml
    └── 05-multi-agent-research.yaml
```

## The three RAG collections

When `build.py` runs, it ingests these into three named knowledge bases inside
the Digitorn RAG module:

| Collection | Source dir | Used by builder for |
|------------|------------|---------------------|
| `digitorn_concepts` | `concepts/` | "What is X?" / "How do I do Y?" |
| `digitorn_modules` | `modules/` | "Which action does Z?" |
| `digitorn_examples` | `examples/` | "Is there a template close to this?" |

The builder agent queries the relevant collection depending on the question
it's trying to answer. Three collections beats one big mixed collection
because top-k search ranks much better when documents are semantically
homogeneous.

## Card format

Every concept card follows this strict template:

```markdown
---
id: trigger-cron
title: "Cron Trigger"
type: concept
keywords: [cron, schedule, hourly, daily, periodic, recurring]
related: [payload-schema, broadcast-routing, session-mode]
source: docs/app-language/09-triggers.md
---

# Cron Trigger

## What it is
One paragraph max. Direct, factual, no fluff.

## When to use
Bullet list. Concrete situations. No "you might want to consider".

## YAML
```yaml
# A minimal, correct snippet that compiles as-is.
triggers:
  - id: hourly
    type: cron
    schedule: "0 * * * *"
```

## Gotchas
Bullet list of real pitfalls - things that bite you in production.
Skip if there are no real gotchas (don't invent them).

## See also
- related-concept-1
- related-concept-2
```

The frontmatter `id` is the filename without `.md`. The `keywords` field
boosts BM25 retrieval on synonyms. The `related` field is currently
informational (the LLM uses it to suggest follow-up reads) but will become a
graph traversal hint later.

## Regeneration workflow

When `docs/` changes:

```bash
# 1. Distill the changed file (or all of them with --all)
python knowledge_base/distill.py docs/app-language/09-triggers.md

# 2. Review the new cards in concepts/ - fix anything off
git diff knowledge_base/concepts/

# 3. Regenerate the auto-generated module cards
python knowledge_base/generate_modules.py

# 4. Rebuild the RAG (drops + re-ingests all 3 collections)
python knowledge_base/build.py

# 5. Commit the cards (the .qdrant store is gitignored)
git add knowledge_base/concepts/ knowledge_base/modules/
git commit -m "kb: refresh after docs update"
```

The Qdrant store is rebuilt from disk on every `build.py` run, so the cards
in this directory are the **single source of truth** - the vector store is a
disposable cache.

## Why distillation, not raw RAG over docs/

Naive chunked RAG over a docs site has two problems for our case:

1. **Cross-file synthesis fails.** When the user asks "how do I send a daily
   email", the right answer combines `09-triggers.md` (cron) +
   `40-channels.md` (email outbound) + an example. A flat vectorial RAG
   ranks each chunk independently and never sees the synthesis.

2. **Documentation tone is wrong for an LLM tool.** Human docs say "Cron
   triggers can be useful when you want to schedule recurring tasks." An
   atomic card says "Use cron when you need a recurring schedule. Don't use
   for event-driven work." The second is what an LLM tool result should look
   like - it makes downstream generation cleaner.

Distilling once, into atomic cards, gives the builder a 10x better retrieval
quality than indexing the raw docs.
