# Skill: Build an Interactive Form

Triggered when the user asks for a form, survey, questionnaire, quiz with
inputs, intake form, feedback form, signup form, etc. Examples:

- "Build me a feedback form"
- "Create a survey to collect info about my sources"
- "I need a registration form with name, email and plan choice"
- "Make a quiz with 5 questions about Anthropic's constitution"

The iframe renders any JSON file under `forms/<slug>.json` as a fully
interactive form (inputs, selects, sections, repeating groups, etc.).
Your job is to emit a JSON schema that matches the contract below.

## Output flow

1. Decide on a slug for the form (kebab-case, derived from the title):
   `feedback`, `team-signup`, `constitution-quiz`.
2. Build the JSON schema (see below).
3. `WsWrite(path="forms/<slug>.json", content=<json>)`.
4. Reply ONE line: `Form ready at forms/<slug>.json. Fill it in the sidebar.`

The user will see the form appear in the Forms group of the iframe
sidebar and can fill + submit. The submission lands at
`responses/<slug>-<iso>.json` and a hint flows back to you on the
user's NEXT message so you can read + analyse the answers.

## JSON contract

```json
{
  "id": "<kebab-slug>",
  "title": "Human title",
  "description": "Optional 1-2 sentence intro shown under the title.",
  "submit_label": "Submit",
  "fields": [ ... see field types below ... ]
}
```

- `id` MUST match the slug in the filename and contain only
  `[a-z0-9-]`. Used as the namespace for response files.
- `title` is mandatory; the user sees it as the form's H1.
- `submit_label` defaults to "Submit" when omitted.

## Field types

Every field has these common keys:

- `id` (required, unique inside the form, `[a-z0-9_]`)
- `type` (required)
- `label` (recommended; the visible text above the input)
- `help` (optional one-line hint shown under the input)
- `show_if` (optional conditional visibility — see below)
- `required` (boolean, applies to input fields)
- `default` (optional initial value)
- `placeholder` (optional, for text-like inputs)

### Text family

```json
{"id": "name", "type": "text", "label": "Full name",
 "required": true, "minLength": 2, "maxLength": 80,
 "placeholder": "Jane Doe"}

{"id": "email", "type": "email", "label": "Email",
 "required": true}

{"id": "homepage", "type": "url", "label": "Personal website"}

{"id": "phone", "type": "tel", "label": "Phone",
 "pattern": "^\\+?[0-9 ]{6,20}$"}

{"id": "bio", "type": "textarea", "label": "Tell us about yourself",
 "rows": 5, "maxLength": 500}
```

### Numbers + ranges

```json
{"id": "age", "type": "number", "label": "Age", "min": 0, "max": 120}
{"id": "satisfaction", "type": "range", "label": "How satisfied?",
 "min": 0, "max": 10, "step": 1, "default": 5}
```

### Dates

```json
{"id": "dob", "type": "date", "label": "Date of birth", "max": "2026-12-31"}
{"id": "appointment", "type": "datetime-local", "label": "Preferred time"}
{"id": "alarm", "type": "time", "label": "Alarm at"}
```

### Choices

```json
{"id": "plan", "type": "select", "label": "Plan",
 "options": [
   {"value": "free", "label": "Free"},
   {"value": "pro",  "label": "Pro ($10/mo)"}
 ]}

{"id": "interests", "type": "multiselect", "label": "Interests (pick any)",
 "options": ["coding", "design", "music"]}

{"id": "color", "type": "radio", "label": "Favourite colour",
 "options": ["red", "blue", "green"]}

{"id": "newsletter", "type": "checkbox", "label": "Subscribe to newsletter"}
```

`options` accepts either a list of strings (used as both value AND
label) or a list of `{value, label}` objects. Prefer the object form
when the storage value differs from the UI label.

### Sections (visual grouping, no data nesting)

Use sections to break a long form into themed blocks. Fields INSIDE
a section keep their flat ids in the response (a section is a layout
hint only — values are NOT nested under the section id).

```json
{
  "id": "section-personal",
  "type": "section",
  "title": "Personal details",
  "description": "Optional intro text for the block.",
  "fields": [
    {"id": "name", "type": "text", "label": "Name"},
    {"id": "email", "type": "email", "label": "Email"}
  ]
}
```

### Repeating groups (arrays of sub-records)

Use a group when the user needs to add MULTIPLE entries of the same
shape (contacts, line items, references). Values are stored as
`[{...}, {...}, ...]` under the group's id.

```json
{
  "id": "contacts",
  "type": "group",
  "title": "Emergency contacts",
  "description": "Add at least one.",
  "add_label": "Add contact",
  "min": 1,
  "max": 5,
  "default_count": 1,
  "fields": [
    {"id": "name", "type": "text", "label": "Name", "required": true},
    {"id": "phone", "type": "tel", "label": "Phone", "required": true},
    {"id": "relation", "type": "select", "label": "Relation",
     "options": ["family", "friend", "colleague"]}
  ]
}
```

`min` / `max` cap the number of instances. `default_count` is how many
empty rows the form opens with (clamped to `>= min` and `<= max`).
`add_label` overrides the "+ Add" button text.

### Conditional visibility (`show_if`)

Show a field only when another field matches a condition. The
referenced field MUST be a sibling in the SAME scope:

- For top-level fields, the scope is the root form values.
- For fields inside a group instance, the scope is THAT instance's
  values (cannot peek into another row).

Operators:

```json
{"show_if": {"field": "contact_method", "equals": "phone"}}
{"show_if": {"field": "plan", "not_equals": "free"}}
{"show_if": {"field": "role", "in": ["admin", "owner"]}}
{"show_if": {"field": "bio", "not_empty": true}}
{"show_if": {"field": "newsletter", "truthy": true}}
```

Hidden fields are NOT validated and their values are NOT submitted.

## Full example

```json
{
  "id": "team-onboarding",
  "title": "New team member onboarding",
  "description": "Tell us a bit about yourself so we can set up your access.",
  "submit_label": "Send to HR",
  "fields": [
    {
      "id": "section-identity",
      "type": "section",
      "title": "Identity",
      "fields": [
        {"id": "full_name", "type": "text", "label": "Full legal name",
         "required": true, "minLength": 2, "maxLength": 100},
        {"id": "email", "type": "email", "label": "Personal email",
         "required": true},
        {"id": "dob", "type": "date", "label": "Date of birth"}
      ]
    },
    {
      "id": "role", "type": "select", "label": "Role",
      "required": true,
      "options": [
        {"value": "eng", "label": "Engineer"},
        {"value": "design", "label": "Designer"},
        {"value": "ops", "label": "Operations"}
      ]
    },
    {
      "id": "github_handle", "type": "text", "label": "GitHub handle",
      "placeholder": "octocat",
      "show_if": {"field": "role", "equals": "eng"}
    },
    {
      "id": "tools", "type": "multiselect", "label": "Tools you need",
      "options": ["VS Code", "Figma", "Notion", "Slack", "Linear"]
    },
    {
      "id": "emergency_contacts", "type": "group",
      "title": "Emergency contacts",
      "min": 1, "max": 3, "add_label": "Add contact",
      "fields": [
        {"id": "name", "type": "text", "label": "Name", "required": true},
        {"id": "phone", "type": "tel", "label": "Phone", "required": true},
        {"id": "relation", "type": "select", "label": "Relation",
         "options": ["family", "friend", "partner", "other"]}
      ]
    },
    {
      "id": "notes", "type": "textarea", "label": "Anything else?",
      "rows": 4, "maxLength": 800
    }
  ]
}
```

## Rules

- Write VALID JSON. The iframe parser fails fast on syntax errors.
- Field ids MUST be unique across the entire form (sections do NOT
  create a new id scope; groups DO).
- For quiz-style forms, store the correct answer in your own memory
  (not in the schema — the user sees the schema). Read the submitted
  response file to grade them.
- Don't add fields the user didn't ask for. Forms are personal data;
  err on the side of minimal collection.
- Keep field labels short (under 60 chars). Use `help` for longer
  explanations.
- Avoid the literal value "null"; use empty string or omit the field.
