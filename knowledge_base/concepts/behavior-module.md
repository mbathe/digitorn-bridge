---
id: behavior-module
title: "behavior Module — runtime rule enforcement + semantic classification"
type: concept
keywords: [behavior, rules, profile, coding, research, data, creative, assistant, classifier, violations, block, warn, remind, pre_tool_check, post_tool_check]
related: [hooks, agent-spawn, capabilities, common-errors]
source: packages/digitorn/modules/behavior/
---

# behavior Module

Runtime enforcement layer that **monitors every tool call** and **classifies every user turn**. Think of it as the guard-rail between an over-eager LLM and your codebase. Unlike `hooks` (which react to events), `behavior` enforces *rules* — it can block a tool before it runs, warn mid-flow, or remind after.

`behavior` takes no capability grant — it's not a tool the LLM calls. It's wired by the agent loop and operates behind the scenes. You configure it purely via the top-level `behavior:` block.

## Two enforcement layers

### 1. Rule engine (always on if enabled)

Monitors every tool call through `pre_tool_check()` and `post_tool_check()`. Each rule has:
- **condition** (what to detect)
- **action** (`block` / `warn` / `remind`)
- **trigger** (which tool names fire the rule)

14 built-in rules (read_before_edit, no_bash_for_files, confirm_destructive (BLOCKS), test_after_changes, verify_after_edit, search_before_read, delegate_complex, always_lint_check, etc.). 5 enforcement profiles (`coding`, `research`, `data`, `creative`, `assistant`) bundle the most relevant rules per app type.

### 2. Semantic classifier (optional — opt in with `classify_turns: true`)

Before the main agent acts, a **small, fast LLM** reads the user's message and classifies it: complexity level, suggested approach, risk. The classification is then injected as a directive into the main agent's context for that turn. Think of it as "supreme coach" that right-sizes the main agent's effort to the actual ask.

## YAML shape

```yaml
behavior:
  profile: coding                 # coding | research | data | creative | assistant
  classify_turns: true            # opt in to classifier (off by default)

  brain:                          # classifier LLM (separate from main agent)
    provider: deepseek
    model: deepseek-chat
    backend: openai_compat
    config:
      api_key: "{{env.DEEPSEEK_API_KEY}}"
    temperature: 0.2
    max_tokens: 2048

  classifier:
    frequency: every_turn         # every_turn | first_turn | skip_followups
    skip_followups: true
    timeout: 25

  rules:                          # override specific built-in rules
    read_before_edit: {enabled: true, enforcement: block}
    no_bash_for_files: {enabled: false}

  custom:                         # add project-specific rules
    - id: "no-prod-writes"
      condition: {type: content_contains, value: "prod.database"}
      action: block
      trigger: tool_start
      message: "Never touch prod.database directly."
```

## Profiles

Each profile is a curated default set. Pick the one that matches the app's role:

- **`coding`** — read_before_edit, verify_after_edit, test_after_changes, search_before_read, always_lint_check. Good for anything that writes or edits code.
- **`research`** — web_search_when_unknown, delegate_large_reads, no_blind_exploration. Good for agents that survey codebases, write reports.
- **`data`** — confirm_destructive (BLOCKS), plan_before_execute. Good for agents running SQL, ETL, migrations.
- **`creative`** — loose-er: no read-before-edit, no lint gates. Good for docs/marketing/copy.
- **`assistant`** — minimal rules, fast path. Good for conversational apps that just chat.

## 3 enforcement levels

- **`block`** — tool call prevented entirely; rule message returned instead of the tool result.
- **`warn`** — tool runs, but a warning message is injected into the conversation.
- **`remind`** — tool runs, no warning mid-flow, but a post-tool hint is added to guide the next turn.

## Session-isolated state

The engine tracks per-session context:
- `read_files` (set of paths the agent has read this session)
- `edited_files` (paths edited without being read first — fodder for `read_before_edit`)
- `reads_since_search` (counter for `no_blind_exploration`)
- `changes_since_test` (counter for `test_after_changes`)

State never crosses sessions — each session starts fresh.

## When to use

- **Code-writing apps**: turn on `coding` profile. Stops read-before-edit class of bugs cold.
- **Production-touching apps**: custom rules with `block` enforcement on dangerous patterns (`no-prod-writes`, `confirm-migrations`).
- **Multi-turn agents**: enable `classify_turns: true` — the classifier's directives shrink over-verbose responses and nudge toward tool calls when appropriate.

## When NOT to use

- Simple one-shot apps or single-agent chat bots — the classifier adds latency per turn for no real gain.
- Background apps firing on cron/webhook — the first-turn user-message assumption doesn't hold.

## See also

- hooks — event-driven automation (different primitive, often used alongside)
- capabilities — what tools an agent can call (behavior enforces *how* it uses them)
- common-errors — many "why did my agent do X" mysteries resolve to a missing behavior rule
