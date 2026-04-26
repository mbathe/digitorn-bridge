---
id: yaml-schema-payloadschemaconfig
title: "PayloadSchemaConfig — YAML schema reference"
type: schema-reference
model: PayloadSchemaConfig
is_root: false
keywords: [payloadschemaconfig, files, metadata, prompt, required]
---

# PayloadSchemaConfig

## Description
Declarative description of the user-pre-filled session payload.

When set on a background app, the Flutter dashboard renders a typed
form (instead of the generic key/value editor) and the daemon can
enforce validation before letting the cron fire on an empty
session. See ``ExecutionConfig.payload_schema``.

Example::

execution:
mode: background
payload_schema:
required: true
prompt:
required: true
label: "What should I look for?"
placeholder: "Find me remote Python jobs paying 80k+"
min_length: 20
metadata:
- name: location
type: string
required: true
label: "City"
- name: min_salary
type: integer
min: 0
default: 60000
- name: remote_only
type: boolean
default: true
files:
- name: cv
label: "Your CV"
required: true
mime: [application/pdf]
max_size_mb: 5
- name: portfolio
required: false
mime: [application/pdf, image/*]
max_count: 5

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `required` | bool |  | `False` | If true, the daemon refuses to fire triggers for a session whose payload doesn't satisfy the schema (missing required prompt / metadata field / file). The dashboard also blocks the 'Activate session' button until the user fills it in. |
| `prompt` | dict[str, any] |  | `{'required': False}` | Prompt field config. Recognised keys: ``required`` (bool), ``label`` (str), ``placeholder`` (str), ``description`` (str), ``default`` (str), ``min_length`` (int), ``max_length`` (int). |
| `metadata` | list[[PayloadFieldConfig](PayloadFieldConfig.md)] |  | `[]` | Typed metadata fields the user fills in via a form. |
| `files` | list[[PayloadFileRuleConfig](PayloadFileRuleConfig.md)] |  | `[]` | File slots with mime/size/count constraints. |

## Linked models
- [PayloadFieldConfig](PayloadFieldConfig.md)
- [PayloadFileRuleConfig](PayloadFileRuleConfig.md)

## Strictness
- `extra: forbid` — unknown keys cause a validation error
