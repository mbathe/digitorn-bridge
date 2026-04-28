---
id: yaml-schema-classifierconfig
title: "ClassifierConfig - YAML schema reference"
type: schema-reference
model: ClassifierConfig
is_root: false
keywords: [classifierconfig, approaches, complexity_levels, context, directive_footer, directive_prefix, frequency, frequency_n, high_risk_threshold, high_risk_warning, max_directives]
---

# ClassifierConfig

## Description
Configuration for the semantic classifier LLM.

The classifier is a generic pre-turn analysis engine. Each app
configures what it analyzes, when it runs, and what it produces.

Example::

behavior:
classify_turns: true
classifier:
frequency: every_turn
timeout: 15
complexity_levels: [trivial, simple, moderate, complex, critical]
approaches: [direct, explore_first, plan_and_confirm, delegate, research_first]
risk_levels: [none, low, medium, high]
max_directives: 5
system_prompt: "{{prompt.classifier}}"

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `frequency` | 'every_turn' \| 'first_turn' \| 'every_n_turns' \| 'on_new_message' |  | `'every_turn'` | When to run the classifier:   'every_turn'    - before every agent turn (classifier can skip via skip_reason)   'first_turn'    - only on the first turn of a session   'every_n_turns' - every N turns (set frequency_n)   'on_new_message'- only when the user sent a new message (skip tool-only turns) |
| `frequency_n` | int |  | `3` | For 'every_n_turns': run every N turns. |
| `skip_followups` | bool |  | `True` | Auto-skip classification for simple follow-ups: 'yes', 'ok', 'continue', 'go ahead', etc. Saves a classifier LLM call. |
| `timeout` | int |  | `15` | Max seconds to wait for the classifier LLM response. |
| `complexity_levels` | list[str \| dict[str, str]] |  | `['trivial', 'simple', 'moderate', 'complex', 'critical']` | Ordered list of complexity levels. Each entry is either a plain string or a dict with {name, when, behavior} for full customization:    complexity_levels:     - name: trivial       when: '1 action, obvious answer'       behavior: 'Just do it, no planning'     - name: deep       when: 'Cross-cutting concern, 10+ files'       behavior: 'Full plan, user approval, phased execution' |
| `approaches` | list[str \| dict[str, str]] |  | `['direct', 'explore_first', 'plan_and_confirm', 'delegate', 'research_first']` | Approach strategies. Each entry is either a plain string or a dict with {name, label, when, behavior} for full customization:    approaches:     - name: direct       label: 'Execute directly'       when: 'Task is trivial or simple, clear path'       behavior: 'Go straight to tool calls, minimal text'     - name: ask_expert       label: 'Needs human expertise'       when: 'Domain knowledge requi... |
| `risk_levels` | list[str \| dict[str, str]] |  | `['none', 'low', 'medium', 'high']` | Risk levels. Same format as approaches - string or dict:    risk_levels:     - name: safe       when: 'Read-only, no side effects'     - name: destructive       when: 'Deletes data, drops tables, force-pushes'       behavior: 'MUST confirm with user, explain what will be lost' |
| `max_directives` | int |  | `5` | Maximum number of directives the classifier should produce. |
| `context` | [ClassifierContextConfig](ClassifierContextConfig.md) |  | `ClassifierContextConfig(tool_inventory=True, session_state=True, workspace_info=True, recent_history=True, history_depth=8)` | What context the classifier receives. |
| `system_prompt` | str \| null |  | `None` | Custom system prompt for the classifier LLM. Supports {{prompt.X}} references to load from ./prompts/. When null, uses the built-in default behavioral model. The prompt receives the output schema dynamically - you don't need to hardcode complexity/approach values in your prompt. |
| `directive_prefix` | str |  | `'[BEHAVIOR DIRECTIVE - {complexity} complexity, {risk} risk]'` | Format string for the directive header. Available placeholders: {complexity}, {approach}, {risk}, {approach_label} |
| `high_risk_warning` | str |  | `'Risk level: {risk}. Confirm destructive or irreversible actions with the user before proceeding.'` | Warning appended when risk >= high_risk_threshold. Use {risk} placeholder. |
| `high_risk_threshold` | str |  | `'medium'` | Risk level (from risk_levels) at or above which high_risk_warning is appended. |
| `directive_footer` | str |  | `'Follow these directives. They are based on your behavioral rules and the current session state. Violations are detected in real-time.'` | Text appended at the end of every directive message. |

## Linked models
- [ClassifierContextConfig](ClassifierContextConfig.md)

## Strictness
- `extra: forbid` - unknown keys cause a validation error
