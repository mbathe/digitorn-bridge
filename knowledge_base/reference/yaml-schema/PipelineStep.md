---
id: yaml-schema-pipelinestep
title: "PipelineStep — YAML schema reference"
type: schema-reference
model: PipelineStep
is_root: false
keywords: [pipelinestep, app, input, optional, output_as]
---

# PipelineStep

## Description
A single step in a pipeline: call a deployed app with an input.

## Fields

| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `app` | str | ✓ | — | Deployed app_id to invoke. |
| `input` | str |  | `''` | Input for this step. Supports {{variables}} including {{input}} (original pipeline input) and {{steps[N].output}} (output of step N). |
| `output_as` | str |  | `''` | Optional name to reference this step's output in later steps. |
| `optional` | bool |  | `False` | If true, continue pipeline even if this step fails. |

## Strictness
- `extra: forbid` — unknown keys cause a validation error
