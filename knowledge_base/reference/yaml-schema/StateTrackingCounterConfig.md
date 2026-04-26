---
id: yaml-schema-statetrackingcounterconfig
title: "StateTrackingCounterConfig — YAML schema reference"
type: schema-reference
model: StateTrackingCounterConfig
is_root: false
keywords: [statetrackingcounterconfig, increment_on, reset_on, reset_when]
---

# StateTrackingCounterConfig

## Description
Configure a named counter.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `increment_on` | list[str] |  | `[]` | Tool names that increment this counter. |
| `reset_on` | list[str] |  | `[]` | Tool names that reset this counter to 0. |
| `reset_when` | dict[str, str] |  | `{}` | Reset when a param matches: {tool, param, matches}. |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
