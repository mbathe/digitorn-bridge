---
id: yaml-schema-behaviorruledefinition
title: "BehaviorRuleDefinition - YAML schema reference"
type: schema-reference
model: BehaviorRuleDefinition
is_root: false
keywords: [behaviorruledefinition, action, condition, description, id, message, trigger, when]
---

# BehaviorRuleDefinition

## Description
A fully declarative behavioral rule - works for ANY action.

Example::

rule_definitions:
- id: read_before_edit
description: "Must read a file before editing it"
trigger: [edit]
when: pre_tool
action: warn
condition:
target_not_in_set: read_files
message: "You are editing '{target}' without reading it first."

- id: no_sql_injection
description: "Block raw SQL in user-facing queries"
trigger: [database.execute]
when: pre_tool
action: block
condition:
param_matches:
param: query
pattern: ".*;\s*(DROP|DELETE|TRUNCATE)"
message: "Dangerous SQL detected. Use parameterized queries."

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `id` | str | ✓ | - | Unique rule identifier. |
| `description` | str |  | `''` | Human-readable description (shown in prompt). |
| `trigger` | list[str] \| str |  | `'*'` | Tool name(s) that trigger this rule. '*' = all tools. |
| `when` | str |  | `'pre_tool'` | When to check: 'pre_tool', 'post_tool', 'on_text' (agent text output). |
| `action` | str |  | `'warn'` | What to do: 'block' (prevent), 'warn' (inject message), 'remind' (post-tool hint). |
| `condition` | dict[str, any] |  | `{}` | When the rule fires. Condition types:   target_not_in_set: <set_name>    - target param NOT in tracked set   target_in_set: <set_name>         - target param IS in tracked set   counter_gte: {name, value}         - counter >= threshold   param_matches: {param, pattern}    - param matches regex   param_contains: {param, value}     - param contains string   flag_is: {name, value}             - fl... |
| `message` | str |  | `''` | Message template. Placeholders:   {target}              - file_path or primary target param   {tool}                - current tool name   {param:<name>}        - any param value   {counter:<name>}      - counter value   {set_count:<name>}    - size of a tracked set   {turn}                - current turn number |

## Strictness
- `extra: forbid` - unknown keys cause a validation error
