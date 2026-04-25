---
id: memory-remember
title: "memory.remember (Remember)"
type: module-action
module: memory
action: remember
fqn: memory.remember
short_name: Remember
keywords: [memory, remember]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# memory.remember (Remember)

## Description
Store a fact that survives context compaction.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `content` | string | ✓ | — | The fact to remember. REQUIRED. Example: remember(content="Test command: pytest tests/ -v") |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: memory
      actions: [remember]
```

## Tool usage instructions
```
Store a fact that survives context compaction. Your long-term memory.

## When to use
- Key findings: 'Auth bug is in src/auth/validate.py:42 — missing null check'
- Architecture decisions: 'Project uses FastAPI + SQLAlchemy + Alembic'
- Important commands: 'Test command: pytest tests/ -v --tb=short'
- After receiving sub-agent results — store the key findings
- After context compaction — re-remember critical info you'll need
- Project structure: 'Entry point: src/main.py, config: src/config.yaml'

## When NOT to use
- Trivial facts you won't need later
- Entire file contents — remember the location, not the content
- Sub-agents should NOT remember goals/tasks — only facts

## Rules
- Keep facts concise (1-2 sentences max)
- Include file paths and line numbers when relevant
- Duplicates are auto-detected and skipped
- Secrets are auto-redacted from stored facts
- Remember AFTER completing work, not before — store results, not plans
```

## Safety
- Risk level: **low**
