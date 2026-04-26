---
id: yaml-schema-statetrackingflagconfig
title: "StateTrackingFlagConfig — YAML schema reference"
type: schema-reference
model: StateTrackingFlagConfig
is_root: false
keywords: [statetrackingflagconfig, set_on, unset_on]
---

# StateTrackingFlagConfig

## Description
Configure a named boolean flag.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `set_on` | list[str] |  | `[]` | Tool names that set this flag to True. |
| `unset_on` | list[str] |  | `[]` | Tool names that set this flag to False. |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
