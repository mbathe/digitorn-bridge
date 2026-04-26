---
id: yaml-schema-inputconfig
title: "InputConfig — YAML schema reference"
type: schema-reference
model: InputConfig
is_root: false
keywords: [inputconfig, accept, description, max_size, required, type]
---

# InputConfig

## Description
Input contract for one_shot mode.

Defines what the application expects as input and how the CLI
should present it to the agent.

Example::

input:
type: text
description: "Code source to analyse"
required: true

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `type` | str |  | `'text'` | Input type: 'text', 'image', 'audio', 'video', 'file', 'json', 'any'. Must be supported by the agent's brain model. For example, 'image' requires a vision-capable model (GPT-4o, Claude Sonnet, Gemini). |
| `accept` | list[str] |  | `[]` | Accepted MIME types. Empty = infer from type. Examples: ['image/png', 'image/jpeg'], ['audio/wav', 'audio/mp3'], ['application/pdf'], ['video/mp4']. |
| `max_size` | str |  | `''` | Maximum input size. Examples: '10MB', '500KB'. Empty = no limit. |
| `description` | str |  | `''` | Human-readable description of the expected input. |
| `required` | bool |  | `True` | Whether input is mandatory. |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
