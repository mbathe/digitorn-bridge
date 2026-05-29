---
id: advanced-06-fallback
title: "Advanced 6 - Brain fallback for billing failover"
sidebar_label: "Advanced 6: Fallback"
---

The most common reason a production agent app stops working
mid-conversation isn't a bug - it's the LLM provider returning a
**402 Insufficient Balance**. The user's credit ran out, the
team's monthly cap was reached, the API key was suspended. The
turn fails, the user sees a generic "provider error", the
session dies.

**Brain fallback** is the production answer. Each agent's brain
can declare a `fallback:` block - a complete second brain config
- that the runtime hot-swaps to whenever the primary returns a
billing error. The fallback is typically a different provider
entirely (a free local model, a fallback API key, a cheaper
plan), so the session keeps running and the user never notices.

## When the failover triggers

The runtime hot-swaps to the fallback brain on **billing-class
errors only**, not arbitrary failures. The trigger conditions
are HTTP 402, the string `"Insufficient Balance"`, and the
substring `"credit"` in the error message. Network errors,
401 auth errors, 5xx server errors do **not** trigger the
failover - those are retried against the primary with
exponential backoff.

The intent is narrow: the failover is meant for **soft-fail
billing situations**, not for disaster recovery. For DR you
want a different mechanism (a frontload load balancer, a
multi-region daemon, a circuit breaker pattern). For "user ran
out of credit", brain fallback is exactly the right shape.

## The YAML

```yaml
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
      max_tokens: 64

      fallback:                              # ← the new piece
        provider: anthropic
        model: claude-haiku-4-5
        backend: anthropic
        config:
          api_key: claude-code               # uses the local OAuth file
        temperature: 0
        max_tokens: 64
```

The `fallback:` block is a complete `AgentBrain` - same fields,
same Pydantic model. Different provider, different model,
different credential. No constraint that the fallback be
"compatible" with the primary; the runtime swaps the entire
brain config, so whatever the fallback declares is what runs.

The example above pairs DeepSeek (cheap, fast, sometimes runs
dry) with Claude Haiku via the Claude Code OAuth alias (no extra
key needed, billed against the local Claude Code subscription).
A common alternative is **the same provider with a different
key** - your team's shared backup key takes over when the
per-user key empties.

## What the runtime actually does

On primary failure, the runtime catches the exception, inspects
the error class and message, and if it matches a billing
signature it:

1. Logs the failover via the daemon logger
   (`llm_billing_exhausted: <primary> → <fallback>` and, on
   success, `llm_billing_fallback_ok: <fallback-model>`).
2. Replaces the live brain instance with the fallback config
   and stashes the primary on the agent context.
3. Retries the LLM call against the fallback.
4. Returns the fallback's response as the turn's reply.

The fallback is **sticky for a 5-minute cooldown**
(`_BILLING_COOLDOWN_S = 300.0`). During
that window every turn talks to the fallback brain so the
daemon doesn't hammer a provider that just rejected us. After
the cooldown the runtime tries the primary again - if the
user's billing recovered, the session quietly returns to it;
if not, another 300 s of fallback usage kicks in.

## Live - the YAML compiles and the primary works

Sample transcript, with both primary and
fallback declared:

```text
> ping

alive
```

`tool_calls_count: 0`, response from the primary brain. The
fallback is loaded into memory but unused on a healthy primary.

To **trigger** the failover you'd need a real 402 from DeepSeek -
deliberately not faked here because faking it correctly requires
a draining a real account or proxying through a fake server. In
production the trigger fires whenever the billing condition
appears; the path through `_handle_llm_error`
is the same exception flow exercised by every cancelled-card
incident operators ever debugged.

## Picking the right fallback

Three patterns work in practice.

**Same provider, backup key.** Your team has a primary
per-user key (which can drain) and a shared service-account
key (which is throttled but never empty). When the user's key
drains, the shared one takes over. Cheapest to set up, no
provider integration needed.

```yaml
fallback:
  provider: deepseek
  model: deepseek-chat
  backend: openai_compat
  credential:
    ref: deepseek_team_backup        # different credential
    scope: per_app_shared
    provider: deepseek
```

**Cheaper model, same provider.** The primary is a flagship
model; the fallback is the cheapest model on the same provider.
If billing fails (rare but possible), at least the session
keeps running with reduced quality.

```yaml
fallback:
  provider: openai
  model: gpt-4o-mini             # cheaper than the primary's gpt-4o
  ...
```

**Different provider entirely.** Most resilient. If the primary
provider has a regional outage or a billing system bug, the
fallback dispatches to a completely separate vendor. Best for
production-critical apps.

```yaml
fallback:
  provider: anthropic
  model: claude-haiku-4-5
  ...
```

The fallback's quality and cost matter less than its
**availability**. A response that's slightly worse than the
primary is better than a session-killing error.

## Composition with other defences

Brain fallback is one of several layers protecting the session
against provider failures. The full stack:

| Layer                    | Catches                                                | Lives in              |
|--------------------------|--------------------------------------------------------|-----------------------|
| Provider SDK retries     | Network errors, 5xx, transient timeouts                | The provider's own SDK config (`max_retries`, `timeout`) |
| Daemon retry loop        | 401 (re-resolves credential), connection errors        |        |
| **Brain fallback**       | **402 / billing / credit errors**                      | **`brain.fallback` in YAML** |
| Capability deny          | Action-level rejection (not LLM-related)               | Capability gate 4      |
| Session-level abort       | User-triggered or daemon-triggered cancellation        | `/sessions/{sid}/abort` |

Each layer handles a distinct failure class. They don't
substitute for each other - brain fallback won't help on a
network outage; SDK retries won't help on a 402.

## Going further

- The full agent brain reference, including the fallback schema:
  [Agents](../language/03-agents.md).
- The `claude-code` API key alias (used in the fallback
  example to read from `~/.claude/.credentials.json` without
  declaring a credential):
  [llm_provider module](../reference/modules/llm_provider.md).
- For multi-app **call chaining** as a different kind of
  resilience pattern (one app's output drives another):
  [Composition](../language/22-composition.md).
