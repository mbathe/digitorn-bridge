---
id: behavior
title: behavior Module
sidebar_label: behavior
---

# behavior

Runtime behavioural enforcement. Monitors every tool call,
detects violations, injects corrections - fully YAML-driven,
no hardcoded logic. Two enforcement layers: a **declarative
rule engine** (14 built-in rules + custom) and an optional
**semantic classifier** (small LLM that injects directives
before the main agent acts).

| Property | Value |
|----------|-------|
| Module id | `behavior` |
| LLM-exposed actions | **0** - operates as a hook on the agent loop |
| Type | per-app instance, per-session state |

> **No agent-callable actions.** This module is wired in
> and called from
> at three points: `classify_turn` at turn
> 0, `pre_tool_check` before each tool,
> `post_tool_check` after each tool. Configuration lives
> entirely in `security.behavior:` in the app YAML.

## Where it sits in the YAML

`security.behavior` - under the canonical
`security:` block:

```yaml
security:
  behavior:
    profile: dev
    classify_turns: true
    rules: { ... }                  # legacy boolean / threshold overrides
    rule_definitions: [ ... ]       # full declarative rules
    state_tracking: { ... }         # custom sets / counters / flags
    classifier: { ... }
    brain: { ... }
```

Full schema + 14 built-in rules + 13 condition primitives +
classifier vocabulary in
[Behavior Engine](../../language/43-behavior.md). Quick
recap below.

## Built-in profiles

 6 presets shipped:

| Profile | Description |
|---------|-------------|
| `dev` | Senior-developer discipline. All sequence + delegation + lint rules ON. Injects 5 KB `DEV_PROMPT_SECTION` into the system prompt. |
| `coding` | `dev` minus `web_search_when_unknown`, slightly looser thresholds, autonomy `high`. |
| `research` | Read-heavy. Disables `read_before_edit` / `test_after_changes` / `verify_after_edit` / `lint_check`. Enables web search + delegation. |
| `data` | Data analysis / ETL. Strict reads + tests + lint, web search ON. |
| `creative` | Writing / content. Low autonomy, `verbosity: detailed`, planning required. |
| `assistant` | General chatbot. Minimal enforcement. |

```yaml
security:
  behavior:
    profile: dev
```

### Custom profiles (bundle directory)

```
my-app/
  app.yaml
  behavior/
    strict_dev.yaml
```

```yaml
# behavior/strict_dev.yaml
name: strict_dev
description: "Production-grade enforcement"
extends: dev
rules:
  max_blind_reads: 1
  changes_before_test_reminder: 1
```

Reference via the `{{behavior.X}}` namespace:

```yaml
security:
  behavior:
    profile: "{{behavior.strict_dev}}"
```

## The 14 built-in rules

 Override any by adding a
`rule_definitions` entry with the same `id`.

### Sequence (5)

| ID | Trigger | When | Action | Effect |
|----|---------|:----:|:------:|--------|
| `read_before_edit` | `edit` | pre | warn | File must be read first. |
| `read_before_write_existing` | `write` | pre | warn | Existing file must be read before overwrite. |
| `search_before_read` | `read` | pre | warn | After 3+ blind reads, suggest Grep / Glob. |
| `verify_after_edit` | `edit` | post | remind | Re-read the modified section. |
| `test_after_changes` | `edit`, `write` | post | remind | Run tests after 3+ changes. |

### Prohibition (3)

| ID | Trigger | When | Action | Effect |
|----|---------|:----:|:------:|--------|
| `no_bash_for_files` | `bash` | pre | warn | Detects `cat`, `head`, `tail`, `less`, `more`, `bat`, `sed`, `awk`, `perl`. |
| `no_blind_exploration` | `bash` | pre | warn | Detects `find .`, `ls -lRa`, `tree`, `dir /s`. |
| `confirm_destructive` | `bash` | pre | **block** | Blocks `rm -rf`, `git reset --hard`, `git push --force`, `git clean -fd`, `drop table`, `drop database`, `truncate table`. |

### Cognitive (6)

| ID | Trigger | When | Action | Effect |
|----|---------|:----:|:------:|--------|
| `plan_before_execute` | `*` | pre | warn | Agent must produce text before the first tool call. |
| `web_search_when_unknown` | `*` | on_text | warn | Detects `not sure / unsure / don't know / uncertain` AND no web search yet. |
| `delegate_complex` | `*` | post | remind | Fires at exactly 8 tool calls in the turn. |
| `delegate_large_reads` | `read` | post | remind | After 5+ sequential reads. |
| `max_sequential_same_tool` | `*` | pre | warn | Same tool 8 times in a row. |
| `always_lint_check` | `edit`, `write` | post | warn | Tool result has lint errors with `severity: error`. |

### Numeric thresholds

| Param | Default | Used by |
|-------|---------|---------|
| `max_blind_reads` | 3 | `search_before_read` |
| `changes_before_test_reminder` | 3 | `test_after_changes` |
| `max_sequential_same_tool` | 8 | `max_sequential_same_tool` |

## Action levels

| Action | Effect | Tool runs? |
|--------|--------|:----------:|
| `block` | `[BEHAVIOR BLOCKED]` injected, tool **prevented**. | no |
| `warn` | `[BEHAVIOR WARNING]` injected, tool runs. | yes |
| `remind` | `[BEHAVIOR REMINDER]` injected after the tool returns. | yes |

## Custom rule definitions

```yaml
security:
  behavior:
    rule_definitions:
      - id: backup_before_modify
        trigger: [database.execute]
        when: pre_tool                # pre_tool | post_tool | on_text
        action: block                 # block | warn | remind
        condition:
          all:
            - param_matches: { param: query, pattern: "(UPDATE|DELETE|DROP)" }
            - flag_is: { name: backup_created, value: false }
        message: "Create a backup before running '{param:query}'."

    state_tracking:
      flags:
        backup_created: { set_on: [database.backup] }
      counters:
        queries_run:    { increment_on: [database.execute] }
```

### Condition primitives

- 13 primitives plus `all` /
`any` / `not` composites.

| Primitive | Behaviour |
|-----------|-----------|
| `target_not_in_set: <set>` / `target_in_set: <set>` | Target param value membership in a tracked set. |
| `counter_gte: {name, value}` | `state.counters[name] >= value`. |
| `flag_is: {name, value}` | `state.flags[name] == value`. |
| `param_matches: {param, pattern}` | Regex on `params[param]`, case-insensitive. |
| `param_contains: {param, value}` | Case-insensitive substring. |
| `no_text_before_tools: true` | Agent didn't produce text before the first tool. |
| `first_tool_this_turn: true` | `state.tool_calls_this_turn == 0`. |
| `consecutive_gte: <N>` | Same tool called N+ times in a row. |
| `tool_calls_this_turn_eq: <N>` | Exactly N tool calls this turn. |
| `target_exists_on_disk: true` | `os.path.exists(target)`. |
| `text_matches: <pattern>` | Regex on agent text (with `when: on_text`). |
| `result_has_lint_errors: true` | Tool result `lint` has any item with `severity: error`. |

## Semantic classifier

`ClassifierConfig`. Optional small LLM that
runs before each main agent turn and emits a
`[BEHAVIOR DIRECTIVE]` block injected into the conversation.

```yaml
security:
  behavior:
    classify_turns: true
    classifier:
      frequency: every_turn          # every_turn | first_turn | every_n_turns | on_new_message
      frequency_n: 3                  # for every_n_turns
      skip_followups: true            # skip "ok", "yes", "continue", ...
      timeout: 15
      complexity_levels: [trivial, simple, moderate, complex, critical]
      approaches: [direct, explore_first, plan_and_confirm, delegate, research_first]
      risk_levels: [none, low, medium, high]
      max_directives: 5
      directive_prefix: "[BEHAVIOR DIRECTIVE - {complexity} complexity, {risk} risk]"
      high_risk_threshold: medium
      directive_footer: "Follow these directives. ..."
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      config: { api_key: "{{secret.DEEPSEEK_API_KEY}}" }
```

## Per-session state

Each session gets its own `BhvSessionState` - sets, counters,
flags, recent tool history, turn number, total tool calls,
violations, consecutive-tool counter. Never cross-contaminate
between sessions.

`cleanup_session(session_id)` runs on session end; called by
the daemon.

## Integration points

| Hook | When | Purpose |
|------|------|---------|
| `classify_turn(...)` | Before turn 0 LLM call | Semantic task classification, inject directive. |
| `on_turn_start(session_id)` | Each turn start | Reset per-turn counters. |
| `pre_tool_check(...)` | Before each tool | Rule evaluation; block if needed. |
| `post_tool_check(...)` | After each tool | State update, reminders. |
| `get_prompt_sections` | System prompt build | Inject active rules list into the prompt. |

## Cross-references

- Full YAML reference + condition primitives + examples by
  domain: [Behavior Engine](../../language/43-behavior.md)
- App-config block reference (`security.behavior`):
  [App Configuration → security](../../language/02-app-config.md)
- Bundle namespaces (where `{{behavior.X}}` resolves from):
  [Bundle namespaces](../../language/38-bundle-namespaces.md)
