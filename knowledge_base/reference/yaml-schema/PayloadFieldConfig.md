---
id: yaml-schema-payloadfieldconfig
title: "PayloadFieldConfig - YAML schema reference"
type: schema-reference
model: PayloadFieldConfig
is_root: false
keywords: [payloadfieldconfig, default, description, label, max, min, name, options, placeholder, required, type]
---

# PayloadFieldConfig

## Description
One declared field on a background app's session payload metadata.

The list of these is what the Flutter dashboard uses to render a
typed form for the user instead of a generic key/value editor.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | str | ✓ | - | Internal key used in payload.metadata. Must be a valid identifier. |
| `label` | str |  | `''` | Human-friendly label shown in the form. Defaults to ``name``. |
| `type` | 'string' \| 'number' \| 'integer' \| 'boolean' \| 'select' \| 'text' |  | `'string'` | Form field type. ``text`` = multiline string. ``select`` requires ``options`` to be set. |
| `required` | bool |  | `False` | Whether this metadata field must be set before activation. |
| `default` | any |  | `None` | Default value pre-filled in the form. |
| `description` | str |  | `''` | Help text shown under the field. |
| `placeholder` | str |  | `''` | Input placeholder. |
| `options` | list[str] |  | `[]` | Allowed values for ``type: select``. |
| `min` | float \| null |  | `None` | Min value for number/integer fields. |
| `max` | float \| null |  | `None` | Max value for number/integer fields. |

## Strictness
- `extra: forbid` - unknown keys cause a validation error
