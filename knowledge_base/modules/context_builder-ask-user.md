---
id: context_builder-ask-user
title: "context_builder.ask_user (AskUser)"
type: module-action
module: context_builder
action: ask_user
fqn: context_builder.ask_user
short_name: AskUser
keywords: [context_builder, ask_user, askuser, meta, interaction, approval, confirm, demander, confirmer, question]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# context_builder.ask_user (AskUser)

## Description
Ask the user a question and WAIT for their response. The agent pauses until the user replies. Supports: simple questions, multiple choices, content review, and structured forms.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `question` | string | ✓ | — | The question or message to show the user. Be specific about what you need. Example: 'Should I proceed with this plan?' |
| `content` | string |  | — | Optional content for the user to review/edit (plan, code, config, etc.). Displayed in a reviewable format. The user can modify it before approving. The (possibly edited) content is returned in the ... |
| `choices` | array |  | — | Optional list of choices for the user to select from. The client displays these as clickable buttons or a dropdown. The user's selection is returned as the response message. Example: ['FastAPI', 'D... |
| `allow_multiple` | boolean |  | `False` | If true with choices, the user can select multiple options. The response will contain all selected choices comma-separated. |
| `form` | array |  | — | Optional structured form for complex user input. Each field: {type, name, label, options?, placeholder?, default?, required?}. Types: 'select', 'text', 'textarea', 'checkbox', 'toggle', 'number'. T... |
| `timeout` | number |  | `300.0` | Max seconds to wait for user response. Default: 300 (5 min). |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: context_builder
      actions: [ask_user]
```

## Tool usage instructions
```
Ask the user a question and WAIT for their response.
The agent pauses completely until the user replies — use this wisely.

## When to use
- Before destructive actions (delete, overwrite)
- When choosing between approaches
- To review a plan or code before proceeding
- When you need specific information the user hasn't provided

## Modes

**Simple question** — just ask:
  ask_user(question="Should I proceed with this plan?")

**With choices** — user clicks a button instead of typing:
  ask_user(question="Which framework?", choices=["FastAPI", "Django", "Flask"])

**Multi-select choices:**
  ask_user(question="Which features?", choices=["Auth", "DB", "Tests", "Docker"], allow_multiple=true)

**Content review** — user sees and can edit a long document:
  ask_user(question="Review and approve this plan.", content="## Plan\n1. Create auth\n2. Add routes\n3. Write tests")

**Structured form** — user fills in multiple fields:
  ask_user(question="Configure the project", form=[
    {"type": "select", "name": "framework", "label": "Framework", "options": ["FastAPI", "Django"]},
    {"type": "text", "name": "name", "label": "Project name", "placeholder": "my-app"},
    {"type": "checkbox", "name": "features", "label": "Features", "options": ["Auth", "DB", "Tests"]},
    {"type": "toggle", "name": "docker", "label": "Docker?", "default": true}
  ])

## Form field types
- select: dropdown with options
- text: single line input
- textarea: multi-line input
- checkbox: multiple select with checkboxes
- toggle: on/off switch
- number: numeric input

## Rules
- Do NOT ask trivial questions — use your best judgment for simple decisions
- Do NOT ask multiple questions in one call — split them
- If the user already gave you enough info, just proceed
- Use choices when there are 2-6 clear options
- Use form when you need multiple pieces of info at once
- Use content when presenting a plan or code for review
```

## Aliases
`confirm`, `demander`, `confirmer`, `approval`, `question`

## Safety
- Risk level: **low**
