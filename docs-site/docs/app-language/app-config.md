---

id: app-config
title: App Configuration
sidebar_position: 2
format: md
---


# App Configuration

The `app:` block is the only required top-level block. It defines your application's identity and runtime limits.

## App Block

```yaml
app:
  name: my-app                    # Required. Unique identifier
  version: "1.0.0"               # Semantic version (default: "1.0.0")
  description: "What this app does"
  author: "your-name"
  tags: [coding, assistant]       # Searchable tags
  license: "MIT"

  # Runtime limits
  max_concurrent_runs: 5          # Max parallel executions (1-100, default: 5)
  max_turns_per_run: 200          # Max agent loop iterations (1-10000, default: 200)
  max_actions_per_turn: 50        # Max tool calls per turn (1-500, default: 50)
  timeout: "3600s"                # Global timeout (default: 1 hour)
  checkpoint: false               # Enable flow checkpoint/resume (default: false)

  # Public interface (optional)
  interface:
    input:
      type: string
      description: "The user's request"
    output:
      type: string
      description: "The agent's response"
    errors:
      - code: TIMEOUT
        description: "Execution exceeded time limit"
      - code: PERMISSION_DENIED
        description: "Action not allowed by security profile"
```

### Field Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | *required* | Application identifier |
| `version` | string | `"1.0.0"` | Semantic version |
| `description` | string | `""` | Human-readable description |
| `author` | string | `""` | Author name |
| `tags` | list[string] | `[]` | Searchable tags |
| `license` | string | `""` | License identifier |
| `max_concurrent_runs` | int | `5` | Max parallel runs (1-100) |
| `max_turns_per_run` | int | `200` | Max agent loop turns (1-10000) |
| `max_actions_per_turn` | int | `50` | Max tool calls per turn (1-500) |
| `timeout` | string | `"3600s"` | Global execution timeout |
| `checkpoint` | bool | `false` | Enable flow state persistence |
| `interface` | object | `{}` | Public contract (input/output/errors) |

## Variables

The `variables:` block defines reusable values accessible throughout the app via `{{variable_name}}`.

```yaml
variables:
  workspace: "{{env.PWD}}"
  max_file_lines: 500
  shell_timeout: "30s"
  output_dir: "{{workspace}}/output"
  languages: "python,typescript,javascript"
```

### Accessing Variables

Variables are accessible anywhere in the YAML via template expressions:

```yaml
agent:
  system_prompt: |
    Working directory: {{workspace}}
    Max lines: {{max_file_lines}}

flow:
  - action: os_exec.run_command
    params:
      command: "ls {{workspace}}"
```

### Built-in Variables

These variables are always available:

| Variable | Description |
|----------|-------------|
| `{{env.VAR_NAME}}` | Environment variable |
| `{{workspace}}` | Alias for `variables.workspace` |
| `{{data_dir}}` | Alias for `variables.data_dir` |
| `{{now}}` | Current Unix timestamp |

## Interface

The `interface:` block documents the app's public contract. It defines what the app accepts as input and returns as output.

```yaml
app:
  interface:
    input:
      type: string
      description: "A coding task or question"
      schema:
        minLength: 1
        maxLength: 10000
    output:
      type: object
      description: "The result with code changes"
      schema:
        properties:
          summary: { type: string }
          files_changed: { type: array }
    errors:
      - code: INVALID_INPUT
        description: "Input was empty or malformed"
      - code: TIMEOUT
        description: "Task exceeded time limit"
```

This is primarily documentation - it helps developers and the API understand what the app expects. The `schema` field follows JSON Schema syntax.

## Custom Types

Define reusable types for structured data:

```yaml
types:
  ReviewFinding:
    severity:
      type: string
      enum: [critical, high, medium, low, info]
      required: true
    file:
      type: string
      required: true
    line:
      type: integer
    message:
      type: string
      required: true
    suggestion:
      type: string
```

Types are available in expression templates and serve as documentation for the data structures your app produces.

## Module Configuration

The `module_config:` block lets you configure module-specific settings. When the app runs in daemon mode, these settings are applied via each module's `on_config_update()` lifecycle hook before execution begins.

```yaml
module_config:
  memory:
    backend: chromadb
    collection: "my-app-memories"
    embedding_model: "all-MiniLM-L6-v2"

  browser:
    headless: true
    viewport_width: 1920
    viewport_height: 1080

  database:
    connection_string: "{{env.DATABASE_URL}}"
    read_only: true
```

### How It Works

1. Before running the app, the runtime iterates over `module_config` entries
2. For each entry, it calls `module.on_config_update(config_dict)` on the corresponding module
3. The module applies the configuration (e.g., changes backend, updates connection settings)
4. Configuration is applied **per-run** - it doesn't permanently change the module's global config

Values support template expressions: `"{{env.DATABASE_URL}}"`, `"{{workspace}}/data"`, etc.

### Available Configuration

Each module defines its own configurable fields. Check the module's documentation or the admin API:

```http
GET /admin/modules/{module_id}/config/schema
```

This returns the JSON schema of configurable fields for the module.

> **Note**: `module_config` only works in daemon mode. In standalone mode, only `filesystem` and `os_exec` are available with their default configurations.

## Complete Example

```yaml
app:
  name: code-reviewer
  version: "2.0"
  description: "AI code review assistant"
  author: llmos
  tags: [code-review, quality]
  max_turns_per_run: 15
  timeout: "600s"
  interface:
    input:
      type: string
      description: "Git diff range or file path to review"
    output:
      type: string
      description: "Structured review report in markdown"

variables:
  workspace: "{{env.PWD}}"
  review_depth: thorough
  max_diff_lines: 2000

types:
  ReviewFinding:
    severity:
      type: string
      enum: [critical, high, medium, low, info]
    file:
      type: string
    message:
      type: string
```
