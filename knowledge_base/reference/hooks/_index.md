---
id: hooks-reference-index
title: "Hooks reference — index"
type: hooks-index
keywords: [hooks, events, conditions, actions, registry, reference, index]
---

# Hooks reference — index

Derived from the hooks registry in `packages/digitorn/core/runtime/hooks.py`. **12 events**, **14 conditions**, **18 actions**.

## Events
See [events.md](events.md) for the full catalogue.

## Conditions (evaluate whether a hook should fire)

- [`all_of`](conditions/all_of.md)
- [`always`](conditions/always.md)
- [`any_of`](conditions/any_of.md)
- [`content_contains`](conditions/content_contains.md)
- [`context_pressure`](conditions/context_pressure.md)
- [`error_type`](conditions/error_type.md)
- [`expression`](conditions/expression.md)
- [`message_count`](conditions/message_count.md)
- [`never`](conditions/never.md)
- [`not`](conditions/not.md)
- [`tool_calls`](conditions/tool_calls.md)
- [`tool_failed`](conditions/tool_failed.md)
- [`tool_name`](conditions/tool_name.md)
- [`turn_count`](conditions/turn_count.md)

## Actions (what the hook does when it fires)

- [`auto_test_deploy`](actions/auto_test_deploy.md)
- [`chain`](actions/chain.md)
- [`compact_context`](actions/compact_context.md)
- [`compile_yaml`](actions/compile_yaml.md)
- [`enforce_compile_fix`](actions/enforce_compile_fix.md)
- [`enforce_phase6`](actions/enforce_phase6.md)
- [`gate`](actions/gate.md)
- [`inject_message`](actions/inject_message.md)
- [`log`](actions/log.md)
- [`lsp_diagnose`](actions/lsp_diagnose.md)
- [`module_action`](actions/module_action.md)
- [`module_action_inject`](actions/module_action_inject.md)
- [`notify`](actions/notify.md)
- [`pipe`](actions/pipe.md)
- [`prefetch_ground_truth`](actions/prefetch_ground_truth.md)
- [`shell`](actions/shell.md)
- [`transform_params`](actions/transform_params.md)
- [`transform_result`](actions/transform_result.md)
