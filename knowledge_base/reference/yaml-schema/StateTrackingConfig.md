---
id: yaml-schema-statetrackingconfig
title: "StateTrackingConfig — YAML schema reference"
type: schema-reference
model: StateTrackingConfig
is_root: false
keywords: [statetrackingconfig, counters, flags, sets]
---

# StateTrackingConfig

## Description
Configure what the session state tracks — fully declarative.

Example::

state_tracking:
sets:
read_files:
add_on: [read, filesystem.read]
target: file_path
fetched_urls:
add_on: [web.fetch]
target: url
counters:
changes_since_test:
increment_on: [edit, write]
reset_on: [bash]
reset_when:
tool: bash
param: command
matches: "pytest|npm test"
flags:
has_web_searched:
set_on: [web.search, search]

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `sets` | dict[str, [StateTrackingSetConfig](StateTrackingSetConfig.md)] |  | `{}` |  |
| `counters` | dict[str, [StateTrackingCounterConfig](StateTrackingCounterConfig.md)] |  | `{}` |  |
| `flags` | dict[str, [StateTrackingFlagConfig](StateTrackingFlagConfig.md)] |  | `{}` |  |

## Linked models
- [StateTrackingCounterConfig](StateTrackingCounterConfig.md)
- [StateTrackingFlagConfig](StateTrackingFlagConfig.md)
- [StateTrackingSetConfig](StateTrackingSetConfig.md)

## Strictness
- `extra: forbid` — unknown keys cause a validation error
