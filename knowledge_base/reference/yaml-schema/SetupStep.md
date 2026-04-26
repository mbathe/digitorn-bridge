---
id: yaml-schema-setupstep
title: "SetupStep — YAML schema reference"
type: schema-reference
model: SetupStep
is_root: false
keywords: [setupstep, action, params]
---

# SetupStep

## Description
A single action call to execute during app bootstrap.

Maps directly to ``module.execute(action, params)``.
The ``params`` dict is validated at compile time against the action's
``params_model`` (Pydantic JSON Schema).

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `action` | str | ✓ | — | Action name on the target module. |
| `params` | dict[str, any] |  | `{}` | Parameters for the action. May contain {{variables}}. |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
