---
id: yaml-schema-payloadfileruleconfig
title: "PayloadFileRuleConfig — YAML schema reference"
type: schema-reference
model: PayloadFileRuleConfig
is_root: false
keywords: [payloadfileruleconfig, description, label, max_count, max_size_mb, mime, name, required]
---

# PayloadFileRuleConfig

## Description
Constraint on the files a user can attach to a session payload.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `name` | str | ✓ | — | Logical slot name (e.g. ``cv``, ``cover_letter``). Free-form. When ``required: true``, the user must upload at least one file matching ``mime`` for this slot. |
| `label` | str |  | `''` | Human-friendly label. |
| `description` | str |  | `''` | Help text shown next to the upload zone. |
| `required` | bool |  | `False` | Whether at least one matching file is mandatory. |
| `mime` | list[str] |  | `[]` | Accepted MIME types (e.g. ``['application/pdf']``). Empty = any. Wildcards like ``image/*`` are supported. |
| `max_size_mb` | float |  | `25.0` | Per-file size cap in MB (server hard cap is 25 MB). |
| `max_count` | int |  | `1` | Max number of files for this slot. |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
