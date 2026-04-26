---
id: yaml-schema-capabilitiesconfig
title: "CapabilitiesConfig — YAML schema reference"
type: schema-reference
model: CapabilitiesConfig
is_root: false
keywords: [capabilitiesconfig, approval_timeout, approve, default_policy, deny, grant, hidden_actions, hidden_modules, max_risk_level]
---

# CapabilitiesConfig

## Description
Application-level security capabilities.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `default_policy` | 'auto' \| 'approve' \| 'block' |  | `'approve'` | Default action policy: 'auto', 'approve', or 'block'. |
| `max_risk_level` | 'low' \| 'medium' \| 'high' |  | `'medium'` | Maximum allowed risk level: 'low', 'medium', or 'high'. |
| `grant` | list[[CapabilityGrant](CapabilityGrant.md)] |  | `[]` | Explicit action grants per module. |
| `approve` | list[[CapabilityGrant](CapabilityGrant.md)] |  | `[]` | Actions requiring explicit user approval before execution. |
| `deny` | list[[CapabilityGrant](CapabilityGrant.md)] |  | `[]` | Explicit action denies per module. |
| `approval_timeout` | int |  | `300` | Seconds to wait for user approval before auto-denying (30–3600). |
| `hidden_modules` | list[str] |  | `[]` | Module IDs to hide from the agent's tool index. Hidden modules are still loaded and can be used by setup steps, hooks, and channels — but the agent cannot see or call their tools. Example: ['filesystem'] to prevent the agent from accessing files. |
| `hidden_actions` | list[[CapabilityGrant](CapabilityGrant.md)] |  | `[]` | Specific actions to hide from the agent's tool index. Unlike 'deny', hidden actions are invisible but still executable by setup steps, hooks, and channels. Use this to declutter the agent's toolset without breaking internal automation. |

## Linked models
- [CapabilityGrant](CapabilityGrant.md)

## Strictness
- `extra: forbid` — unknown keys cause a validation error
