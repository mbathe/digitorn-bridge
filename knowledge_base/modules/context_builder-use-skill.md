---
id: context_builder-use-skill
title: "context_builder.use_skill (UseSkill)"
type: module-action
module: context_builder
action: use_skill
fqn: context_builder.use_skill
short_name: UseSkill
keywords: [context_builder, use_skill, useskill, skills, primitive]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.use_skill (UseSkill)

## Description
Load a skill -- a reusable workflow with detailed instructions. Skills provide step-by-step methodology for specific tasks (e.g. /commit, /review, /security-audit). The skill content is returned so you can follow its instructions.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `command` | string | ✓ | — | Skill command to load (e.g. '/commit', '/review') |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [use_skill]
```

## Safety
- Risk level: **low**
