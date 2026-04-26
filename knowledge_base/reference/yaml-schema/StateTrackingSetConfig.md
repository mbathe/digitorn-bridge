---
id: yaml-schema-statetrackingsetconfig
title: "StateTrackingSetConfig — YAML schema reference"
type: schema-reference
model: StateTrackingSetConfig
is_root: false
keywords: [statetrackingsetconfig, add_on, aliases, target]
---

# StateTrackingSetConfig

## Description
Configure a named set that tracks targets per tool.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `add_on` | list[str] | ✓ | — | Tool names that add to this set. |
| `target` | str |  | `'file_path'` | Param name to extract as the target value. |
| `aliases` | list[str] |  | `[]` | Alternative param names (path, filepath, etc.). |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
