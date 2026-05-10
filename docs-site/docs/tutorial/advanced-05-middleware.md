---
id: advanced-05-middleware
title: "Advanced 5 - Middleware pipeline"
sidebar_label: "Advanced 5: Middleware"
---

The capabilities gate decides which actions the agent **can** call.
The behaviour engine decides **how** they're called. Middleware
sits at a third layer: it wraps **the LLM call itself**, before
the request leaves the daemon and after the response comes back.

That's where you intercept secrets the user pasted into the
prompt, block dangerous content patterns, inject retrieval
context from a knowledge base, or attach a content classifier.
None of those involve a tool call - they're all transformations
of the message stream around the model.

Three built-in middlewares cover the most common needs:
**`mask_secrets`** scrubs sensitive patterns out of user input
and assistant output, **`content_filter`** short-circuits the
LLM call entirely on dangerous patterns, and **`rag_inject`**
prepends retrieved context to the prompt. You can also chain
custom middleware loaded from a Python file or installed
package.

## How the pipeline works

For each LLM call the runtime walks the middleware list:

1. `before()` hooks run **in declaration order**.
2. If any `before()` returns a string, it **short-circuits** -
   the LLM call doesn't happen and that string becomes the
   reply.
3. Otherwise the LLM call executes.
4. `after()` hooks run **in reverse order**, each potentially
   modifying the response.

So `[mask_secrets, content_filter, response_filter]` declared in
that order runs `mask_secrets.before` → `content_filter.before` →
`response_filter.before` → LLM → `response_filter.after` →
`content_filter.after` → `mask_secrets.after`. Reverse order on
the way back lets the outermost middleware see the final shape.

## The YAML

Save this as `middleware-bot.yaml`. Two middlewares are wired:
`mask_secrets` and `content_filter`. The agent's only job is to
echo whatever you type back at it - the interesting behaviour
comes from the middleware.

```yaml
app:
  app_id: middleware-bot
  name: Middleware Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 4
  timeout: 60
  middleware:
    - mask_secrets:
        patterns: [api_key, password, token, secret, bearer]
        mask_values: true
    - content_filter:
        block_patterns: ["DROP TABLE", "rm -rf /", "DELETE FROM users"]
        rejection_message: "This request was blocked by content_filter."

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
      max_tokens: 200
    system_prompt: |
      Echo the user's message verbatim. Do not paraphrase. Add no
      commentary. Just the message back.

tools:
  modules: {}
  capabilities:
    default_policy: auto
```

The system prompt asks the agent to echo whatever the user typed.
Without middleware, that's exactly what would happen. With
middleware, the model sees a transformed input.

## Live - mask_secrets

Real session captured against the daemon.

```text
> Echo this verbatim: my api_key=sk-abc123def456 and my password=hunter2

my [MASKED] and my [MASKED]
```

The user pasted both an `api_key=...` and a `password=...`
pattern. `mask_secrets.before()` matched both against the
configured patterns, replaced the **values** with `[MASKED]`, and
the model received only the masked text. The model echoed what
it saw - which is exactly the protection: the secret never
reached the LLM provider's logs, never landed in the persistent
event log, never showed up in the assistant message either.

The default pattern set covers `password`, `api_key`, `token`,
`bearer`, plus `sk-*`, `ghp_*`, `glpat-*` (Stripe / GitHub /
GitLab token prefixes). The YAML above adds custom patterns to
the list; the built-in set is preserved.

## Live - content_filter

```text
> Echo this verbatim: DROP TABLE users; SELECT * FROM secrets;

This request was blocked by content_filter.
```

The user message matched `DROP TABLE` from the
`block_patterns` list. `content_filter.before()` raised the
short-circuit, the LLM call **never happened**, and the
configured `rejection_message` became the reply. Zero tokens
billed for that turn. The session log records the event with
`error: "[CONTENT FILTER] Pattern matched: DROP TABLE"` for
forensics.

`content_filter` runs after `mask_secrets`, so secrets are
already scrubbed when the patterns are matched. Order matters
here - putting `content_filter` before `mask_secrets` would let
patterns that contain literal secret values slip through the
filter even though they'd never reach the model anyway.

## The other built-ins

**`prompt_inject`** - dynamically inject content into the system
prompt at LLM call time. Useful when the system instruction
depends on something only known at runtime (current user,
day-of-week, A/B test cohort).

```yaml
- prompt_inject:
    system: "Always respond in French."
    position: append            # append (default) | prepend
```

**`rag_inject`** - retrieve relevant chunks from a knowledge
base and prepend them to the system prompt. Combines well with
the [RAG bot](../howtos/build-a-rag-bot.md) pattern but works at
the middleware layer instead of the agent layer:

```yaml
- rag_inject:
    max_chunks: 5
    max_chars: 2000
    position: append
    collection: my-docs
```

**`response_filter`** - cap the response length and apply secret
masking on output. Belt-and-suspenders pairing for
`mask_secrets`; if the model regenerated a secret on its own
output, this scrubs it on the way back.

```yaml
- response_filter:
    max_length: 5000
    mask_secrets: true
```

## Custom middleware

Drop a Python file with a class that implements `before()` and
optionally `after()`:

```python
# middlewares/my_middleware.py
class MyAppMiddleware:
    def __init__(self, key: str = "default"):
        self.key = key

    async def before(self, ctx):
        # Modify ctx.system_prompt, ctx.messages, etc.
        # Return a string to short-circuit (becomes the reply).
        return None

    async def after(self, ctx, response, tool_calls):
        # Modify the response before it goes back to the user.
        return response
```

Then reference it:

```yaml
runtime:
  middleware:
    - custom:
        path: ./middlewares/my_middleware.py
        class: MyAppMiddleware
        config:
          key: production
```

Resolution order is **TOML registry** (middleware packages
installed via `digitorn middleware install`) → **inline
fallback** (the built-ins shipped with the daemon). Path or
package overrides both.

The full middleware reference (every built-in's parameters,
the module-level vs MCP-level layers, the `ctx` object's API)
is in [Middleware](../language/17-middleware.md).

## Where each layer fits

The four protective layers compose, not compete:

| Layer                  | Surface                  | When it fires                                       |
|------------------------|--------------------------|-----------------------------------------------------|
| Capability gate        | Per tool call            | Before the action's Python method runs              |
| Behaviour engine       | Per tool call (pattern)  | Around the action call, with rule-based decisions   |
| **Middleware**         | **Per LLM call**         | **Around the model's request/response**             |
| OS sandbox             | Per worker process       | At the kernel layer, around every syscall the worker issues |

Middleware is the right tool when the protection isn't about
**which** action is called but **what content** flows through
the LLM stream. Secret scrubbing, content filtering, RAG
injection, prompt augmentation - all of those live here.

## Going further

- Full middleware reference (every built-in, the module-level
  and MCP-level layers, custom-middleware authoring):
  [Middleware](../language/17-middleware.md).
- The sister concept at the **tool-call** level instead of the
  LLM-call level:
  [Tool hooks](../language/31-tool-hooks.md).
- For stronger protections than pattern matching, combine
  with the behaviour engine:
  [Security 3 - Custom rule](security-03-custom-rule.md).
