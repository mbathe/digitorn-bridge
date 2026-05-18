---
id: advanced-08-composition
title: "Advanced 8 - App composition with call_app"
sidebar_label: "Advanced 8: Composition"
---

A multi-agent app fans work out across **specialists inside one
app**. App composition fans work out across **separate, fully
deployed apps**. Each app can be deployed, versioned, and
sandboxed independently; the **`call_app`** primitive lets one
agent invoke another app in `one_shot` mode and read back its
result as a tool call.

This is the right shape when:

- The downstream app is **shared** across several callers (a
  translator used by 5 different products).
- The downstream app needs its **own credentials** (a Stripe
  refunder app that holds the Stripe key separately from
  every caller).
- The downstream app has a **different security profile** -
  stricter sandbox, different behaviour rules.
- You want **independent versioning** - upgrading the
  downstream app upgrades it for every caller without
  touching the callers.

## How call_app works

`call_app` is a built-in action exposed by the
`context_builder` module (auto-loaded). The agent calls it
with two params:

```python
call_app(app_id="<target_id>", input="<text the target receives>")
```

The runtime:

1. Looks up the target app on the daemon. The target must be
   **deployed** and in **`one_shot` mode**.
2. Spawns a fresh session for the target with the input as
   the user message.
3. Runs the target to completion (or until its `timeout` /
   `max_turns` cap).
4. Returns the target's `runtime.output` as the call_app
   result.

The caller sees one tool call (`CallApp`) in its session log.
The target's session log is independent - one row per call,
reachable through the daemon's session-history API for that app.

## The two YAMLs

### Target - `translator-app.yaml`

```yaml
app:
  app_id: translator-app
  name: Translator App
  version: "1.0"

runtime:
  mode: one_shot                        # required for call_app targets
  workdir_mode: none
  max_turns: 2
  timeout: 60
  input:
    type: text
    description: "English text to translate"
    required: true
  output:
    type: text
    description: "French translation"

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
      max_tokens: 256
    system_prompt: |
      Translate the user's input to French. Output only the
      translation, no preamble, no quotes.

tools:
  modules: {}
  capabilities:
    default_policy: auto
```

The `runtime.input` and `runtime.output` blocks are the
contract: callers pass text, the app produces text. JSON
schemas can be declared in `output.schema` for stricter
validation.

### Caller - `composer-bot.yaml`

```yaml
app:
  app_id: composer-bot
  name: Composer Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 4
  timeout: 60

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
      max_tokens: 300
    system_prompt: |
      You are a coordinator. To translate any English text to
      French, call call_app with app_id="translator-app" and
      pass the text as input. After getting the result, present
      it to the user with the prefix "FR:". Be concise.

tools:
  modules: {}
  capabilities:
    default_policy: auto
    grant:
      - module: context_builder           # the meta module
        actions: [call_app]                # explicit grant for composition
```

The explicit `grant` for `context_builder.call_app` is
important. With `default_policy: auto` alone, the action is
filtered out of the agent's tool list because it's flagged as
a meta-action. Granting it explicitly puts it back in.

## Live transcript

Sample transcript, with both apps deployed:

```text
> Translate to French: "The world is changing fast."

FR: Le monde change rapidement.
```

The captured tool call:

```python
CallApp(
  app_id="translator-app",
  input="The world is changing fast."
)
# success: true
# result: { app_id: "translator-app",
#           output: "Le monde change rapidement.",
#           tool_calls: 0 }
```

`tool_calls_count: 1` from the composer's perspective. Inside
that single tool call, a complete LLM call against the
translator app ran (the translator made zero tool calls of its
own - it just formed the translation directly).

The composer received `output: "Le monde change rapidement."`
back, prepended `"FR: "` per its system prompt, and replied to
the user.

## Independent sessions

The translator app's session is **separate** from the composer's.
Two consequences:

- The translator does **not** see the composer's conversation
  history. It only sees the `input` it was passed.
- The translator's tool calls don't count against the composer's
  `max_turns`. The composer used 1 turn (the CallApp); the
  translator used 1 turn internally. They don't sum.

This isolation is the whole point. The composer can call the
translator a thousand times in a long session without the
translator's context window growing.

## Pipeline mode

For sequential composition (app A's output feeds app B's input
without a coordinator agent), use `runtime.mode: pipeline`
instead of writing a coordinator. The pipeline declarative
shape is:

```yaml
runtime:
  mode: pipeline
  pipeline:
    - app_id: extractor
      input: "{{user.message}}"
    - app_id: classifier
      input: "{{steps.extractor.output}}"
    - app_id: summarizer
      input: "{{steps.classifier.output}}"
```

Each step's output feeds the next step's input. No agent loop
runs in pipeline mode - it's a deterministic chain. Useful for
ETL-shaped workflows where the chain is fixed and the LLM
isn't deciding which app to call next.

## Resilience patterns

A composer app gets resilient when it pairs `call_app` with
**brain fallback** ([Advanced 6](advanced-06-brain-fallback.md)):
the composer's primary brain might fail with 402, but the call
to the target app uses the target's own brain config (with its
own fallback). One billing failure on the composer's side
doesn't kill calls to the target - the call_app dispatch lives
inside the agent loop's billing-failover scope.

For network-class failures (target app crashed mid-call,
network partition), `call_app` returns `success: false` with a
structured error and the composer's agent sees it as a normal
tool failure. Wrap the call in the **behaviour engine's
`retry`** rule to auto-retry, or let the agent decide what to
do based on the error message.

## Going further

- The full app-composition reference (input/output schemas,
  pipeline mode, error semantics):
  [Composition](../language/22-composition.md).
- The `context_builder` module that exposes `call_app` plus
  the discovery meta-tools:
  [context_builder reference](../reference/modules/context_builder.md).
