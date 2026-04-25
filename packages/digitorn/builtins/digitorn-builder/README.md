# Digitorn App Builder

Build new Digitorn apps from a natural-language description.

## What it does

You tell it what you want — *"a hourly job-board scraper that uses
my CV"* — and it walks you through:

1. **Discovery** — a few targeted questions to understand the goal
2. **Pattern match** — does an existing template fit? (one of 5
   archetypes: scheduled monitor, conversational assistant, event
   webhook processor, document pipeline, multi-agent research)
3. **Generation** — assembles the YAML, either by adapting the
   template or composing from scratch using the RAG knowledge base
4. **Compile loop** — sends the YAML to the daemon's
   ``/api/discovery/compile`` endpoint, reads errors, fixes them,
   re-compiles. Up to 5 attempts.
5. **Persist** — saves the work as a draft so you can come back later
6. **Deploy** — only with explicit consent, never silently

## What's inside

- **3 RAG knowledge bases** (concepts, modules, examples) loaded
  from ``./.digitorn/knowledge_base/.qdrant``
- **HTTP client** for calling the daemon's discovery + builder
  routes (``/api/discovery/compile``, ``/api/builder/drafts/*``)
- **Memory** for the state-machine and per-build scratch
- **Filesystem** for local YAML preview
- **context_builder.ask_user** for structured questions with
  choices, forms, and content review

## Why a meta-app?

Building an agent app today means writing YAML by hand. That's
fine if you know the framework deeply — frustrating if you don't.
Digitorn Builder is the on-ramp: it lets a non-expert describe
what they want and end up with a working, validated, deployable
app.

The builder eats Digitorn's own dogfood — every tool it uses
(rag, http, ask_user, filesystem, memory) is a regular Digitorn
module. The system prompt orchestrates them via a state machine.

## Permissions

- ✅ Network (calls localhost daemon API + RAG queries)
- ✅ Filesystem (writes YAML preview locally)
- ❌ Shell execution
- 🟡 Risk level: **medium**

## Sister packages

For the builder to work end-to-end, the knowledge base must be
populated. Run::

    py -3.12 knowledge_base/build.py

This is normally done at daemon startup, but you can re-run it
after editing the ``knowledge_base/`` source files.
