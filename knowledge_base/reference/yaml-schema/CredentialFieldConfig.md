---
id: yaml-schema-credentialfieldconfig
title: "CredentialFieldConfig - YAML schema reference"
type: schema-reference
model: CredentialFieldConfig
is_root: false
keywords: [credentialfieldconfig, default, description, help, label, name, options, placeholder, required, type, validation_regex]
---

# CredentialFieldConfig

## Description
One field inside a credential provider (e.g. ``api_key``, ``bot_token``).

Directly mapped to the form widget the Flutter client renders.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | str | ✓ | - | Internal field name (identifier). |
| `label` | str |  | `''` | Human label shown in the form. |
| `type` | 'secret' \| 'string' \| 'url' \| 'select' \| 'number' \| 'boolean' \| 'connection_string' |  | `'secret'` | Form widget type. ``secret`` = masked password field, ``url`` = URL input with validation, ``select`` requires ``options``, ``connection_string`` = URL with scheme/host check. |
| `required` | bool |  | `False` |  |
| `default` | any |  | `None` | Pre-filled default value. |
| `description` | str |  | `''` | Help text. |
| `placeholder` | str |  | `''` | Input placeholder. |
| `validation_regex` | str |  | `''` | Optional regex the value must match. Validated both server-side (handler) and client-side (form). |
| `options` | list[str] |  | `[]` | Allowed values for ``type: select``. |
| `help` | str |  | `''` | Extra inline help shown below the input. |

## Strictness
- `extra: forbid` - unknown keys cause a validation error
