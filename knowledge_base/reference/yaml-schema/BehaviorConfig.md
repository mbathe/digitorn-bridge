---
id: yaml-schema-behaviorconfig
title: "BehaviorConfig — YAML schema reference"
type: schema-reference
model: BehaviorConfig
is_root: false
keywords: [behaviorconfig, brain, classifier, classify_turns, custom, profile, rule_definitions, rules, state_tracking, use_agent_brain]
---

# BehaviorConfig

## Description
Behavioral enforcement rules — actively monitored at runtime.

Define a profile preset and/or individual rules. All enabled rules
are enforced by the behavior engine on every tool call.

Example::

behavior:
profile: coding
classify_turns: true
classifier:
frequency: every_turn
timeout: 15
approaches: [direct, plan_and_confirm, delegate]
rules:
read_before_edit: true
test_after_changes: true
custom:
- id: protect_migrations
rule: "Never modify migration files without asking"
trigger: edit
action: block

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `profile` | str \| null |  | `None` | Preset profile: 'dev', 'coding', 'research', 'data', 'creative', 'assistant', or '{{behavior.X}}'. |
| `rules` | dict[str, any] |  | `{}` | Rule overrides. Keys are rule IDs (read_before_edit, test_after_changes, etc.), values are bool or int. |
| `custom` | list[[BehaviorCustomRule](BehaviorCustomRule.md)] |  | `[]` | Legacy custom rules (backward compat). Prefer rule_definitions. |
| `rule_definitions` | list[[BehaviorRuleDefinition](BehaviorRuleDefinition.md)] |  | `[]` | Fully declarative rules — works for ANY action. See BehaviorRuleDefinition. |
| `state_tracking` | [StateTrackingConfig](StateTrackingConfig.md) \| null |  | `None` | What the session state tracks. When null, uses defaults from profile. |
| `classify_turns` | bool |  | `False` | Enable semantic classification. A small LLM analyzes each user message BEFORE the main agent acts and injects behavioral directives. |
| `classifier` | [ClassifierConfig](ClassifierConfig.md) |  | `ClassifierConfig(frequency='every_turn', frequency_n=3, skip_followups=True, timeout=15, complexity_levels=['trivial', 'simple', 'moderate', 'complex', 'critical'], approaches=['direct', 'explore_first', 'plan_and_confirm', 'delegate', 'research_first'], risk_levels=['none', 'low', 'medium', 'high'], max_directives=5, context=ClassifierContextConfig(tool_inventory=True, session_state=True, workspace_info=True, recent_history=True, history_depth=8), system_prompt=None, directive_prefix='[BEHAVIOR DIRECTIVE — {complexity} complexity, {risk} risk]', high_risk_warning='Risk level: {risk}. Confirm destructive or irreversible actions with the user before proceeding.', high_risk_threshold='medium', directive_footer='Follow these directives. They are based on your behavioral rules and the current session state. Violations are detected in real-time.')` | Configuration for the semantic classifier LLM. |
| `brain` | ForwardRef("'AgentBrain \| None'") |  | `None` | LLM for semantic classification. Uses the same AgentBrain format as agents. If omitted, uses the coordinator's brain. Tip: use a fast/cheap model (haiku, deepseek-chat) for minimal latency. |
| `use_agent_brain` | bool |  | `True` | When brain is not set, reuse the coordinator's brain for classification. Set to false to disable classification when no brain is configured. |

## Linked models
- [BehaviorCustomRule](BehaviorCustomRule.md)
- [BehaviorRuleDefinition](BehaviorRuleDefinition.md)
- [ClassifierConfig](ClassifierConfig.md)
- [StateTrackingConfig](StateTrackingConfig.md)

## Strictness
- `extra: forbid` — unknown keys cause a validation error
