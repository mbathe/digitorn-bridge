---
id: behavior
title: behavior
sidebar_label: behavior
---

# behavior

Runtime behavioral enforcement module. Monitors every tool call, detects violations, and injects corrections into the conversation — fully YAML-driven, no hardcoded logic.

Two enforcement layers:

1. **Rule engine** — evaluates declarative rules before and after every tool call. Can block, warn, or remind the agent.
2. **Semantic classifier** (optional) — a small LLM analyzes each user message before the main agent acts, classifies the task, and injects behavioral directives.

> This module has **no agent-callable actions** — it operates transparently as a hook on the agent loop. It is wired in `bootstrap.py` and called from `agent_loop.py` at three points: `classify_turn()` at turn 0, `pre_tool_check()` before each tool, and `post_tool_check()` after each tool.

---

## YAML configuration

```yaml
behavior:
  profile: coding           # built-in profile (see Profiles section)
  classify_turns: true      # enable semantic classifier
  brain:                    # LLM for classification (small/fast model recommended)
    provider: anthropic
    model: claude-haiku-4-5
    config:
      api_key: "claude-code"
  rules:                    # override specific built-in rule flags
    test_after_changes: false
    max_sequential_same_tool: 5
  custom:                   # extra custom rules (appended on top of profile)
    - id: no_drop
      trigger: [shell.bash]
      when: pre_tool
      action: block
      condition:
        param_matches: {param: command, pattern: "DROP TABLE"}
      message: "DROP TABLE is not allowed."
```

---

## Built-in profiles

Five profiles ship out of the box. Each is a preset of boolean rule flags and thresholds:

| Profile | Description |
|---------|-------------|
| `dev` | Ultra-strict: read before edit, search before read, plan before execute, delegate complex, verify after edit, lint always |
| `coding` | Standard coding: read before edit, search before read, test after changes, no bash for files |
| `research` | Research-oriented: web search when unknown, delegate large reads, no bash for files |
| `data` | Data analysis: verify after edit, test after changes |
| `creative` | Minimal enforcement — creativity over process |
| `assistant` | General assistant: light enforcement |

Reference a profile:

```yaml
behavior:
  profile: coding
```

### Custom profiles (bundle directory)

Custom profiles live in `behavior/` alongside `app.yaml`:

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
extends: dev          # inherit from built-in

rules:
  max_blind_reads: 1
  changes_before_test_reminder: 1
```

Reference with the `{{behavior.X}}` namespace:

```yaml
behavior:
  profile: "{{behavior.strict_dev}}"
```

---

## Built-in rules

14 built-in rules, toggled with boolean flags in `rules:`:

| Rule flag | Default (coding) | Description |
|-----------|-----------------|-------------|
| `read_before_edit` | `true` | Warn if agent edits a file it hasn't read this session |
| `read_before_write_existing` | `true` | Warn if agent writes over an existing file without reading |
| `search_before_read` | `true` | Warn if agent reads files without searching first |
| `no_bash_for_files` | `true` | Block `shell.bash` calls that use `cat`, `sed`, `awk` etc. on files (use filesystem tools instead) |
| `no_blind_exploration` | `true` | Warn if agent reads too many files without searching |
| `confirm_destructive` | `false` | Block destructive operations until confirmed |
| `plan_before_execute` | `false` | Remind agent to plan before executing complex tasks |
| `verify_after_edit` | `false` | Remind agent to verify files after editing |
| `test_after_changes` | `false` | Remind agent to run tests after code changes |
| `delegate_complex` | `false` | Remind agent to spawn sub-agents for complex tasks |
| `delegate_large_reads` | `false` | Remind agent to delegate when reading many files |
| `web_search_when_unknown` | `false` | Remind agent to search the web for unknown information |
| `always_lint_check` | `false` | Remind agent to run lint after every edit |
| `max_sequential_same_tool` | `false` | Block if agent calls the same tool more than N times in a row |

### Thresholds

| Parameter | Default (coding) | Description |
|-----------|-----------------|-------------|
| `max_blind_reads` | `3` | Max consecutive reads without a search before `no_blind_exploration` fires |
| `changes_before_test_reminder` | `3` | Number of edits before `test_after_changes` fires |
| `max_sequential_same_tool` | `10` | Max consecutive same-tool calls before blocking |

---

## Semantic classifier

When `classify_turns: true`, a small LLM runs before each main agent turn and injects behavioral directives based on task complexity, approach, and risk level.

```yaml
behavior:
  classify_turns: true
  brain:
    provider: anthropic
    model: claude-haiku-4-5
    config:
      api_key: "claude-code"
  classifier:
    frequency: every_turn       # every_turn | first_turn_only | adaptive
    skip_followups: true        # skip classification on follow-up messages
    timeout: 15                 # max seconds for classification LLM call
    include_tool_inventory: true
    include_session_state: true
```

The classifier output is a structured `{ complexity, approach, risk_level, directives[] }` object. Directives are formatted and injected as a system message before the agent's first LLM call.

---

## Enforcement levels

| Level | Behavior |
|-------|----------|
| `block` | Tool execution is **prevented**. A `violation` message is injected into the conversation. |
| `warn` | Tool executes, but a warning is injected before the result. |
| `remind` | Tool executes, and a reminder is appended after the result. |

---

## Custom rule definitions

Full custom rules with arbitrary state:

```yaml
behavior:
  rule_definitions:
    - id: backup_before_modify
      trigger: [database.execute]
      when: pre_tool             # pre_tool | post_tool
      action: block              # block | warn | remind
      condition:
        all:
          - param_matches: {param: query, pattern: "(UPDATE|DELETE|DROP)"}
          - flag_is: {name: backup_created, value: false}
      message: "Create a backup before running '{param:query}'."

  state_tracking:
    flags:
      backup_created:
        set_on: [database.backup]
    counters:
      queries_run:
        increment_on: [database.execute]
```

### Condition operators

| Operator | Description |
|----------|-------------|
| `all` | All sub-conditions must match (AND) |
| `any` | At least one sub-condition must match (OR) |
| `not` | Negates a sub-condition |
| `param_matches` | Tool param matches a regex pattern |
| `param_contains` | Tool param contains a substring |
| `flag_is` | A tracked session flag equals a value |
| `counter_gt` / `counter_gte` | A tracked counter exceeds a threshold |
| `tool_name_in` | Tool name is in a list |

---

## Session isolation

The behavior engine maintains per-session state: `read_files`, `edited_files`, `reads_since_search`, `changes_since_test`. State never leaks between sessions.

Cleanup is called by the daemon on session end:

```python
await behavior_module.cleanup_session(session_id)
```

---

## Integration points

The module is wired via `bootstrap.py::_wire_behavior_module()` and called from `agent_loop.py`:

| Hook | When | Purpose |
|------|------|---------|
| `classify_turn(session_id, message, ...)` | Before turn 0 LLM call | Semantic task classification, inject directives |
| `on_turn_start(session_id)` | Each turn start | Reset per-turn counters |
| `pre_tool_check(session_id, tool_name, params)` | Before each tool | Rule evaluation, block if needed |
| `post_tool_check(session_id, tool_name, params, result)` | After each tool | State update, reminders |
| `get_prompt_sections()` | System prompt build | Inject active rules list into system prompt |

---

## See also

- [app-language/43-behavior.md](../../app-language/43-behavior.md) — full YAML reference with examples
- [app-language/33-rules.md](../../app-language/33-rules.md) — built-in rule definitions
