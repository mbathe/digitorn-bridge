# Digitorn Deep Research

A multi-agent research team that takes a question and produces a
structured, cited report.

## What it does

You give it a research question - *"Compare the top 3 vector
databases for production RAG"* - and it runs the following
pipeline:

1. **Coordinator** decomposes the question into 2-4 distinct angles
2. **Web researchers** spawn in **parallel**, one per angle, each
   doing its own web.search + web.fetch loop
3. **Fact checker** runs in parallel with the researchers,
   verifying load-bearing claims about the topic against
   authoritative sources
4. **Coordinator joins** all results via ``agent_wait_all`` and
   collects the findings into a single buffer
5. **Writer** synthesises the findings into a structured markdown
   report with TL;DR, background, themes, open questions, sources
6. **Editor** polishes the draft, catches contradictions, removes
   uncited claims, returns the final version
7. The final report is saved to ``./reports/<timestamp>.md`` and
   returned to the user

## Why it ships built-in

Deep Research is the canonical demonstration of multi-agent
parallelism in Digitorn. It uses every facet of the
``agent_spawn`` module - fan-out, wait_all, result collection -
and shows that 5 agents working in parallel beat 1 agent doing
sequential web searches both in **speed** and in **rigor**.

## What's inside

- **5 agents** (coordinator + 4 specialists)
- **agent_spawn** for parallel orchestration
- **web** for live information lookups
- **memory** for shared goal context across workers
- **filesystem** for writing the final report
- **one_shot mode** - invoked via ``POST /api/apps/digitorn-deepresearch/run``
  with the research question as the input

## Permissions

- ✅ Network (web search + fetch)
- ✅ Filesystem write (only ``./reports/``)
- ❌ Shell execution
- 🟡 Risk level: **medium**

## Customisation

Like all built-ins, copy the directory and edit it to make your
own variant - e.g. add a translator agent for multi-language
reports, or swap the writer's output format from markdown to
HTML.
