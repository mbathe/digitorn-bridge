---
id: howtos-rag
title: Build a RAG bot
sidebar_label: RAG bot
---

A RAG bot answers questions from a folder of documents. The agent
indexes the folder once, then on each question retrieves the
relevant chunks and uses them as context for the reply. Citations
make it easy to verify what the agent claims.

This how-to puts together three things from the tutorials:
**memory** isn't needed (RAG keeps state in its own knowledge
base), **tools** are the `rag.*` actions, and the agent loop is
unchanged.

## What you'll build

- A folder of three small markdown files about a fictional SaaS
  product (pricing, onboarding, refund policy).
- A YAML app `rag-bot` that uses the `rag` module.
- A two-question session where the agent retrieves the right file
  and quotes from it.

## Prerequisites

- A running daemon (with the gateway component up; embeddings go
  through it). `digitorn start` covers both.
- An authenticated user in the daemon.
- A `deepseek_main` credential (or whatever provider you prefer)
  in the per-user vault. Adjust the `brain` block to taste.

## Sample data

Three markdown files in a single directory.

```bash
mkdir -p ./rag-data
```

`rag-data/pricing.md`:

```markdown
# Pricing notes

Our SaaS plans:
- Starter: $29/month, 1 user, basic features
- Team: $99/month, 5 users, all features
- Enterprise: custom pricing, contact sales

Annual plans get 20% off. We do NOT offer free trials.
```

`rag-data/onboarding.md`:

```markdown
# Onboarding flow

New users go through:
1. Signup form (email + password, no SSO yet)
2. Email verification (link expires in 24h)
3. Workspace creation (one workspace per account)
4. Invite teammates (Team plan and above)

Average completion time: 8 minutes. Drop-off is highest at step 2.
```

`rag-data/refunds.md`:

```markdown
# Refund policy

Full refunds are issued within 30 days of purchase, no questions
asked. After 30 days, refunds are pro-rated for the unused portion
of the billing period. Annual plans cancelled mid-year are refunded
the remaining months minus a 10% admin fee.
```

## The YAML

```yaml
app:
  app_id: rag-bot
  name: RAG Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 8
  timeout: 180

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
      temperature: 0
      max_tokens: 512
    system_prompt: |
      You answer questions about a knowledge base named "docs".

      On the FIRST turn the knowledge base may not exist yet. If
      the user asks you to index something, do it in this order:
        1. rag.create_knowledge_base(name="docs")
        2. rag.ingest_directory(knowledge_base="docs", path=<the path>,
           extensions=[".md"])
      Then confirm in one short sentence.

      On follow-up turns, call rag.search(knowledge_base="docs",
      query=<the user question>) and answer based on the retrieved
      chunks. Cite the file name in your reply, e.g. (from pricing.md).

tools:
  modules:
    rag: {}
  capabilities:
    default_policy: auto

ui:
  greeting: "Ask me to index a folder, then ask questions about its content."
```

## Deploy and chat

```bash
digitorn dev deploy rag-bot.yaml
digitorn dev chat rag-bot
```

Pick `./rag-data` as the workspace when
the dev CLI prompts.

## Live transcript

Three real turns against a running daemon. The agent created a
fresh knowledge base, ingested the three markdown files, then
answered two follow-up questions with citations.

### Turn 1 - index the folder

```text
> Index everything under ./rag-data into
  a knowledge base called 'docs'.

Done! I've indexed everything under
`./rag-data` into the **docs** knowledge
base. It found 3 documents (markdown files) and created 3 chunks.
You can now ask me questions about the content and I'll retrieve
answers from the knowledge base.
```

The agent fired three tool calls during this turn
(`tool_calls_count: 3`): one `rag.create_knowledge_base`, one
`rag.ingest_directory`, and one stats call to confirm the load.

### Turn 2 - retrieve from refunds.md

```text
> What's the refund policy after 30 days?

Here's what the refund policy says for after 30 days (from
**refunds.md**):

- **Within 30 days**: Full refund, no questions asked.
- **After 30 days**: Refunds are **pro-rated** for the unused
  portion of the billing period.
- **Annual plans** cancelled mid-year: You get a refund for the
  remaining months **minus a 10% admin fee**.
```

Five tool calls this turn (`tool_calls_count: 5`): the agent
called `rag.search` once for the main question, then a few
follow-up retrievals to enrich the answer with the within-30-day
case and the annual-plan case.

### Turn 3 - retrieve from pricing.md

```text
> How much is the Team plan?

The **Team plan** is **$99/month** for up to **5 users** with all
features included. If you go annual, you get **20% off**
(from **pricing.md**).
```

One tool call (`tool_calls_count: 1`) - a single `rag.search`
was enough to find the right chunk in `pricing.md`.

## What's actually happening

The default `rag` module pipeline does **hybrid retrieval**: it
runs both BM25 (keyword) and semantic (vector) search in parallel,
fuses the results with reciprocal-rank fusion, and returns the
top-k chunks. With only three small files the fusion barely
matters, but the same pipeline scales to thousands of documents
without configuration changes.

The chunking strategy is `recursive` by default - the splitter
prefers paragraph boundaries, falls back to sentence boundaries,
then to fixed-size as a last resort. Override with
`chunking: { strategy: paragraph, size: 1000 }` under the rag
module config if your documents are unusual.

## What the system prompt is doing

The two-step prompt (`create_knowledge_base` then `ingest_directory`)
is the safe ordering: ingest before create returns an "unknown
collection" error and the agent has to retry. Telling the model
the order in plain English saves a turn.

The "cite the file name" rule turns RAG from a black box into
something verifiable. The user can `cat pricing.md` and check the
quote line by line. Without the citation rule the agent often
paraphrases without attribution.

## Hardening for production

The above YAML is fine for a demo. Production setups want:

- **Persistent backend**. The default rag module is in-memory and
  loses everything on daemon restart. Switch to Qdrant on disk
  with `tools.modules.rag.config.backend: { type: qdrant, path:
  "/var/digitorn/qdrant" }`. See the
  [rag module reference](../reference/modules/rag.md).
- **Reranker**. Enable a cross-encoder reranker
  (`pipeline.rerank_top_n: 20`) to improve precision when the
  document set grows.
- **Multi-query expansion**. Set
  `pipeline.multi_query: { enabled: true, provider: <llm>,
  num_variants: 3 }` so a single question fans out into three
  paraphrases - catches more chunks at the cost of a small LLM
  call per turn.
- **Citation verification**. With `citations: { verify: true }`
  the daemon post-checks that every quoted span in the answer
  comes from the retrieved chunks. Hallucinated citations get
  flagged.

Each of these is one config change in the YAML. The
[Advanced RAG](../language/37-rag.md) page documents the full
configuration surface.

## Cross-references

- [rag module reference](../reference/modules/rag.md) - every
  action, parameters, return shapes.
- [Advanced RAG](../language/37-rag.md) - the pipeline knobs:
  hybrid weights, reranker, semantic cache, Text2SQL, CRAG,
  adaptive routing.
- [Vector module](../reference/modules/vector.md) - lower level,
  raw collections without the RAG pipeline.
