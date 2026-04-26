---
id: yaml-schema-appmeta
title: "AppMeta — YAML schema reference"
type: schema-reference
model: AppMeta
is_root: false
keywords: [appmeta, app_id, author, category, color, description, features, icon, name, quick_prompts, schema_version]
---

# AppMeta

## Description
Top-level application identity.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `app_id` | str | ✓ | — | Unique application identifier. |
| `name` | str | ✓ | — | Human-readable application name. |
| `version` | str |  | `'1.0'` | Application version string. |
| `schema_version` | str |  | `'1'` | YAML schema version for forward compatibility. |
| `description` | str |  | `''` | Optional description. |
| `author` | str |  | `''` | Application author. |
| `tags` | list[str] |  | `[]` | Searchable tags. |
| `icon` | str |  | `''` | App icon. Can be: emoji ('💻'), icon name ('code'), URL to an image ('https://...'), or base64 data URI. If empty, the client generates a colored circle from app_id. |
| `color` | str |  | `''` | Accent color for the app card/header. Hex format: '#8B5CF6'. If empty, auto-generated from app_id hash. |
| `category` | str |  | `'general'` | App category for grouping in the UI. Examples: 'coding', 'writing', 'research', 'data', 'devops', 'design', 'communication', 'automation', 'general'. |
| `quick_prompts` | list[dict[str, str]] |  | `[]` | Suggested prompts shown as clickable buttons when the user opens the app. Each entry: {label: 'short text', message: 'full prompt', icon: 'emoji'}. If empty, the client shows just the input field. |
| `features` | dict[str, bool] |  | `{}` | Client-UI feature toggles (same contract as top-level features:). Keys: voice, attachments, tools_panel, snippets, tasks_panel, memory_panel, context_ring, markdown, slash_commands, message_actions, status_pills, token_badges. Unspecified keys default to true on the client. |
| `theme` | dict[str, str] |  | `{}` | Client theme overrides. Keys: accent (hex), background (hex). accent overrides app.color for fine-grained control. |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
