---
id: payload-schema
title: "Payload schema (declarative session configuration for background apps)"
type: concept
keywords: [payload_schema, prompt, metadata, files, form, background, session, validation, required, select, text, number, integer, boolean, mime, max_size_mb, max_count]
related: [session-modes, execution-modes, triggers]
source: packages/digitorn/core/app/schema.py
---

# Payload schema -- declarative session configuration

## What it is

The payload schema declares **what information the user provides when creating a background session**. It defines a typed form with three sections: a main **prompt**, typed **metadata** fields, and **file** upload slots. The Flutter dashboard renders this schema as a form, and the daemon validates payloads before allowing trigger activation.

Only meaningful in `mode: background`, typically with `session_mode: multi`.

## YAML reference

```yaml
execution:
  mode: background
  payload_schema:
    required: true              # Block activation until payload is valid
    prompt:
      required: true
      label: "What should I monitor?"
      placeholder: "Track BTC price on Binance and alert me when it changes by 5%"
      description: "Describe what the agent should do"
      default: ""
      min_length: 20
      max_length: 2000
    metadata:
      - name: field_name
        type: string            # string, number, integer, boolean, select, text
        label: "Field Label"
        required: true
        default: "default value"
        description: "Help text"
        placeholder: "Enter value..."
        options: []             # For type: select only
        min: 0                  # For number/integer only
        max: 100                # For number/integer only
    files:
      - name: slot_name
        label: "Upload Label"
        description: "Help text for upload"
        required: false
        mime: ["application/pdf"]
        max_size_mb: 5
        max_count: 1
```

## Prompt section

The main user instruction for the agent.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `required` | bool | false | Whether a prompt must be provided |
| `label` | string | "Prompt" | Label shown in the form |
| `placeholder` | string | "" | Placeholder text |
| `description` | string | "" | Help text below the field |
| `default` | string | "" | Pre-filled default value |
| `min_length` | int | 0 | Minimum character count |
| `max_length` | int | unlimited | Maximum character count |

## Metadata section

A list of typed fields rendered as a form. Each field has:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Internal key in `payload.metadata` (must be valid identifier) |
| `type` | string | "string" | `string`, `number`, `integer`, `boolean`, `select`, `text` |
| `label` | string | name | Human label shown in the form |
| `required` | bool | false | Whether this field must be filled |
| `default` | any | null | Pre-filled default value |
| `description` | string | "" | Help text below the field |
| `placeholder` | string | "" | Input placeholder |
| `options` | list[string] | [] | Allowed values for `type: select` |
| `min` | float | null | Min value for number/integer |
| `max` | float | null | Max value for number/integer |

### Field types

| Type | Widget | Notes |
|------|--------|-------|
| `string` | Single-line text input | |
| `text` | Multi-line textarea | For longer text |
| `number` | Number input with decimals | Supports `min`/`max` |
| `integer` | Number input, integers only | Supports `min`/`max` |
| `boolean` | Toggle switch | Default: false |
| `select` | Dropdown | Requires `options` list |

## Files section

File upload slots with MIME type and size constraints.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Logical slot name (e.g., `cv`, `data`) |
| `label` | string | name | Human label |
| `description` | string | "" | Help text next to upload zone |
| `required` | bool | false | At least one file must be uploaded |
| `mime` | list[string] | [] | Accepted MIME types. Empty = any. Wildcards: `image/*` |
| `max_size_mb` | float | 25.0 | Per-file size cap in MB |
| `max_count` | int | 1 | Max files for this slot |

## How payload reaches the agent

When a trigger fires on a session with a payload:

1. The `prompt` is delivered as the user message (combined with the trigger message).
2. The `metadata` fields are available in the agent's context via memory or system prompt injection.
3. Uploaded `files` are accessible via the filesystem module.

The agent reads the payload values from its auto-injected memory snapshot or from the trigger message template.

## Validation behavior

When `required: true` on the payload_schema:

- The daemon **refuses to fire triggers** for a session whose payload doesn't satisfy the schema
- The Flutter dashboard **blocks the "Activate session" button** until the user fills in all required fields
- Missing required prompt, metadata fields, or files cause validation failure

## Examples

### Job search monitor (template 01 pattern)

```yaml
execution:
  mode: background
  session_mode: multi
  payload_schema:
    required: true
    prompt:
      required: true
      label: "What kind of jobs are you looking for?"
      placeholder: "Find me remote Python jobs paying 80k+ in Berlin"
      min_length: 20
    metadata:
      - name: location
        type: string
        required: true
        label: "City"
        placeholder: "Berlin"
      - name: min_salary
        type: integer
        label: "Minimum salary"
        min: 0
        default: 60000
      - name: remote_only
        type: boolean
        label: "Remote only?"
        default: true
      - name: job_type
        type: select
        label: "Contract type"
        options: ["full-time", "part-time", "contract", "freelance"]
        default: "full-time"
    files:
      - name: cv
        label: "Your CV"
        required: true
        mime: [application/pdf]
        max_size_mb: 5
      - name: portfolio
        label: "Portfolio samples"
        required: false
        mime: [application/pdf, "image/*"]
        max_count: 5
        max_size_mb: 10
  triggers:
    - id: check
      type: cron
      schedule: "0 * * * *"
      message: "Check for new job listings matching the session criteria"
```

### Simple monitor with just a prompt

```yaml
execution:
  mode: background
  session_mode: multi
  payload_schema:
    required: true
    prompt:
      required: true
      label: "What should I monitor?"
      placeholder: "Track the price of ETH on CoinGecko"
  triggers:
    - id: check
      type: cron
      schedule: "*/15 * * * *"
      message: "Run the monitoring check"
```

### Data pipeline with file upload

```yaml
execution:
  mode: background
  session_mode: multi
  payload_schema:
    required: true
    prompt:
      required: false
      label: "Processing instructions"
      placeholder: "Clean and normalize the data, remove duplicates"
    metadata:
      - name: output_format
        type: select
        label: "Output format"
        options: ["csv", "json", "parquet"]
        default: "csv"
      - name: delimiter
        type: string
        label: "CSV delimiter"
        default: ","
        placeholder: ","
    files:
      - name: input_data
        label: "Data file to process"
        required: true
        mime: ["text/csv", "application/json", "application/vnd.ms-excel"]
        max_size_mb: 25
        max_count: 1
  triggers:
    - id: process
      type: watch
      paths: ["./inbox/*.csv", "./inbox/*.json"]
      message: "New file to process: {{event.path}}"
```
