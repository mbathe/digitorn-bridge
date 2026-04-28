---
id: yaml-schema-outputconfig
title: "OutputConfig - YAML schema reference"
type: schema-reference
model: OutputConfig
is_root: false
keywords: [outputconfig, description, format, schema_def, type]
---

# OutputConfig

## Description
Output contract for one_shot mode.

Defines what the application produces and how the CLI should
format it.

Example::

output:
type: json
description: "Structured analysis report"
schema:
type: object
properties:
bugs: { type: array }
score: { type: integer }

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `type` | str |  | `'text'` | Output type: 'text', 'json', 'markdown', 'file', 'image', 'audio'. Determines how the CLI and API format the response. |
| `format` | str |  | `''` | Output format hint. For 'json': a JSON Schema. For 'file': the file extension. For 'image': 'png', 'svg', etc. |
| `description` | str |  | `''` | Human-readable description of the output. |
| `schema_def` | dict[str, any] |  | `{}` | Optional JSON Schema for the expected output structure. |

## Strictness
- `extra: forbid` - unknown keys cause a validation error
