---
id: yaml-schema-capabilitygrant
title: "CapabilityGrant — YAML schema reference"
type: schema-reference
model: CapabilityGrant
is_root: false
keywords: [capabilitygrant, actions, module, reason]
---

# CapabilityGrant

## Description
An explicit grant or deny for module actions.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `module` | str | ✓ | — | Target module ID. |
| `actions` | list[str] |  | `[]` | Action names. Empty = all actions on the module. |
| `reason` | str |  | `''` | Human-readable reason (for deny). |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
