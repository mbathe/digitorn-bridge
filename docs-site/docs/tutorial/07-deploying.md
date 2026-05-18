---
id: tutorial-07-deploying
title: "7. Deploying"
sidebar_label: "7. Deploy"
---

The first six tutorials all stayed in dev mode: `default_policy:
auto`, every action available, broad timeouts, no behaviour rules.
Production needs the opposite: **deny-by-default**, an explicit
allowlist, a behaviour profile, a credentials contract, and
context compaction so a long-running session doesn't blow the
token budget.

This page walks through one **production-shape YAML** and proves
that each piece does what it claims to do against a live daemon.

## Prerequisites

Same daemon and credential as the previous tutorials. The vault
already holds the `deepseek_main` credential from the earlier
turns.

## The YAML

Save this as `prod-bot.yaml`:

```yaml
app:
  app_id: prod-bot
  name: Production Bot
  version: "1.0"
  description: Production-shape app with deny-by-default capabilities, behavior profile, and credential schema.
  category: assistant
  author: digitorn

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 20
  timeout: 60
  hooks:
    - id: compact_when_full
      "on": turn_end
      condition:
        type: context_pressure
        threshold: 0.75
      action:
        type: compact_context
        keep_recent: 6

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
      context:
        max_tokens: 32000
        strategy: summarize
        keep_recent: 8
    system_prompt: |
      You are a production assistant. Reply concisely. When the
      user asks for a file, use Read; when asked about the codebase,
      use Glob and Grep. Never write or delete; never run shell.

tools:
  modules:
    filesystem: {}
    memory: {}
  capabilities:
    default_policy: block
    max_risk_level: medium
    grant:
      - module: filesystem
        actions: [read, glob, grep]
      - module: memory
        actions: [remember, set_goal, task_create, task_update]
    deny:
      - module: filesystem
        actions: [write, edit]
    approval_timeout: 300

security:
  behavior:
    profile: assistant
    classify_turns: false
  credentials_schema:
    required: true
    providers:
      - name: deepseek_main
        label: DeepSeek
        type: api_key
        scope: per_user
        fields:
          - name: api_key
            type: secret
            required: true

ui:
  greeting: "Production bot - read-only filesystem, summarising context, behaviour profile applied."
```

The differences from a dev-mode app sit in five places.

**`runtime.hooks`** declares a context-compaction hook that fires
on `turn_end` when the token pressure crosses 75 % of the brain's
context window. The `compact_context` action summarises old turns
and keeps the last six raw, so the session can run for hours
without falling over.

**`runtime.timeout: 60`** caps how long a single turn can take.
The dev-default of 300 s is fine while you debug; production wants
a tighter ceiling so a stuck provider doesn't pin a worker thread.

**`tools.capabilities.default_policy: block`** flips the allowlist
the right way around. Nothing the agent calls runs unless an
explicit `grant:` row matches. The `deny` rows below are belt and
suspenders - even a future schema change that adds `filesystem.write`
to the auto-grant set would still be blocked.

**`security.behavior.profile: assistant`** loads the assistant
behaviour profile - a predefined set of rules that the
[behaviour engine](../language/43-behavior.md) enforces around
every tool call. The profile is read-only safe; for code-editing
apps you'd pick `coding` instead.

**`security.credentials_schema`** declares which credentials the
app needs to run. The the chat client / web client uses this to render a
"Connect your DeepSeek account" form when the user installs the
app. The compiler also uses it to validate every `credential.ref`
in the YAML, catching typos at compile time rather than at the
first agent call.

## Proof 1 - the YAML compiles and runs

Deploy and send a one-word probe:

```text
> Reply with one short word: ready
ready
```

Real result captured against the daemon: `tool_calls_count: 0`,
`turns_used: 1`, completion in under two seconds. The brain works,
the credential schema validated, the compaction hook is armed.

## Proof 2 - the deny actually denies

Try to make the agent write a file:

```text
> Write a file at /tmp/x.txt with content 'ok' using the Write tool.

I don't have a `Write` tool available. The tools I have are:
Read, Glob, Grep, TaskCreate, TaskUpdate, MemorySetGoal,
Remember, background_run, and run_parallel.

As per my instructions: "Never write or delete; never run shell."
- I cannot create or write files.
```

The agent saw only the explicitly granted tools (`Read`, `Glob`,
`Grep`, the four memory tools, plus the always-injected
`background_run` and `run_parallel` primitives). `Write` and
`Edit` are not in the tool list because they were `deny`'d at
the capability layer. The system never had to refuse a call -
the call could not be expressed in the first place.

If the agent had been clever and tried to invoke an unknown tool
name, the seven-gate security pipeline would have rejected it at
gate 1 (module access) or gate 4 (policy). The
[security reference](../language/11-security.md#how-a-tool-call-is-gated---the-seven-gates)
documents the gate sequence in detail.

## Other things you'll want

The YAML above covers the application layer. Production also
needs:

- **TLS in front of the daemon**. The daemon speaks HTTP; put it
  behind nginx, Caddy, or a managed load balancer with a real
  certificate. Configuration knobs (`server.host`,
  `server.port`, `server.cors`) live in the
  [daemon configuration](../language/23-configuration.md).
- **Auth enabled**. Set `server.auth_enabled: true` and require
  Bearer tokens on every `/api/*` route. The five
  always-public paths are listed in the
  [API reference](../reference/api/).
- **OS-level sandbox**. On Linux that's Landlock + seccomp; on
  macOS, Seatbelt; on Windows, Job Objects. Declare it under
  `security.sandbox`. The
  [OS Sandbox reference](../language/35-sandbox.md) explains
  per-platform availability and the four profile presets
  (`dev`, `standard`, `strict`, `maximum`).
- **Persistent state**. The default `memory` and `rag` modules
  are in-process. For multi-replica deployments swap them for
  database-backed storage; both modules accept a `backend:`
  block to point at Postgres / Redis / Qdrant.
- **Rate limits**. The daemon ships with per-IP and per-user
  rate limits configured under
  [`daemon.rate_limit`](../language/23-configuration.md).
  Tighten or loosen depending on traffic profile.
- **Observability**. Health probes, JSON metrics, and per-session
  metrics are documented in
  [Observability](../language/24-observability.md).

These are config-shaped concerns, not YAML-app concerns, so the
tutorial doesn't repeat them - the linked pages have the full
details with concrete `digitorn.config.yaml` examples.

## What you have at the end of the tutorial

By stitching all seven steps together you end up with a Digitorn
deployment that:

- Compiles the same YAML in dev and prod (one block of overrides
  swapped at deploy time, not a fork)
- Runs read-only by default and only widens the surface where the
  app explicitly asks
- Scales context with summarising compaction so a session can
  run for hours
- Drives a workspace pane the user can read, approve, and reject
  per file
- Spawns specialists for parallel work and falls back gracefully
  when one fails
- Wakes itself on a cron / webhook / file watcher with a real
  trigger payload
- Holds credentials in a typed, encrypted vault that the client
  can render a form for

That's the full end-to-end shape. The
[Reference](../reference/) and [Language](../language/) sections
are where the granular knobs live; come back to them whenever
the framework's behaviour surprises you.
