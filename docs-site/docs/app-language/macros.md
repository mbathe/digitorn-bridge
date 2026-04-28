---

id: macros
title: Macros
sidebar_position: 9
format: md
---


# Macros

Macros are reusable flow snippets. Define them once, invoke them anywhere in your flow with different parameters.

## Defining a Macro

```yaml
macros:
  - name: read_and_summarize
    description: "Read a file and produce a summary"
    params:
      path:
        type: string
        required: true
      max_lines:
        type: integer
        default: 200
    body:
      - id: read_file
        action: filesystem.read_file
        params:
          path: "{{macro.path}}"
      - id: summarize
        agent: default
        input: |
          Summarize this file (first {{macro.max_lines}} lines):
          {{result.read_file}}
```

## Using a Macro

Invoke a macro in a flow with the `use:` keyword and pass parameters with `with:`:

```yaml
flow:
  - id: summarize_config
    use: read_and_summarize
    with:
      path: "{{workspace}}/config.json"
      max_lines: 100

  - id: summarize_readme
    use: read_and_summarize
    with:
      path: "{{workspace}}/README.md"
```

## Macro Parameters

### Parameter Types

| Type | Aliases | Description |
|------|---------|-------------|
| `string` | | Text value |
| `int` | `integer` | Integer number |
| `float` | `number` | Decimal number |
| `bool` | `boolean` | True/false |
| `object` | | JSON object |
| `array` | `list` | JSON array |

### Required vs Optional

```yaml
macros:
  - name: run_test
    params:
      command:
        type: string
        required: true           # Must be provided
      working_dir:
        type: string
        required: false
        default: "{{workspace}}" # Default value if not provided
```

### Shorthand Syntax

For simple defaults, use shorthand:

```yaml
macros:
  - name: greet
    params:
      name: "World"             # Inferred as string, required=false, default="World"
      loud: false               # Inferred as bool, required=false, default=false
```

## Accessing Parameters

Inside a macro body, access parameters with `{{macro.param_name}}`:

```yaml
macros:
  - name: run_command
    params:
      command:
        type: string
        required: true
      dir:
        type: string
        default: "."
    body:
      - id: exec
        action: os_exec.run_command
        params:
          command: "{{macro.command}}"
          working_directory: "{{macro.dir}}"
```

## Macro Body

The body is a list of flow steps - exactly the same as the top-level `flow:` block. All 18 step types are supported inside macros.

### Action Steps

```yaml
body:
  - id: step1
    action: filesystem.read_file
    params:
      path: "{{macro.path}}"
```

### Agent Steps

```yaml
body:
  - id: analyze
    agent: reviewer
    input: "Analyze: {{result.step1}}"
```

### Branching

```yaml
body:
  - id: check
    branch:
      "on": "{{result.exec.exit_code}}"
      cases:
        "0":
          - agent: default
            input: "Success!"
      default:
        - agent: default
          input: "Failed: {{result.exec.stderr}}"
```

### Nested Macros

Macros can invoke other macros:

```yaml
macros:
  - name: ensure_dir
    params:
      dir: { type: string, required: true }
    body:
      - action: os_exec.run_command
        params: { command: "mkdir -p {{macro.dir}}" }

  - name: save_report
    params:
      dir: { type: string, required: true }
      content: { type: string, required: true }
    body:
      - id: mkdir
        use: ensure_dir
        with: { dir: "{{macro.dir}}" }
      - action: filesystem.write_file
        params:
          path: "{{macro.dir}}/report.md"
          content: "{{macro.content}}"
```

## Result Scoping

Step results inside a macro are scoped to the macro invocation. The macro's overall result is the result of its last step.

```yaml
flow:
  - id: review
    use: read_and_summarize
    with:
      path: "config.json"

  # {{result.review}} contains the output of the macro's last step
  - id: use_summary
    agent: default
    input: "Based on the summary: {{result.review}}"
```

## Validation

The compiler validates macro references at compile time:

- Macro names must be unique
- `use:` must reference a defined macro name
- Unknown macro references produce a `CompilationError`

## Real-World Examples

### Git Diff Macro

```yaml
macros:
  - name: git_diff
    description: "Get git diff for analysis"
    params:
      range:
        type: string
        default: "HEAD~1..HEAD"
    body:
      - id: get_diff
        action: os_exec.run_command
        params:
          command: "git diff {{macro.range}} --stat && echo '---' && git diff {{macro.range}}"
          working_directory: "{{workspace}}"
```

### Parallel Analysis Macro

```yaml
macros:
  - name: parallel_lint
    description: "Run multiple linters in parallel"
    params:
      targets:
        type: string
        default: "."
    body:
      - parallel:
          max_concurrent: 3
          fail_fast: false
          steps:
            - id: ruff
              action: os_exec.run_command
              params: { command: "ruff check {{macro.targets}}" }
            - id: mypy
              action: os_exec.run_command
              params: { command: "mypy {{macro.targets}}" }
            - id: bandit
              action: os_exec.run_command
              params: { command: "bandit -r {{macro.targets}}" }
```

### Search and Summarize Macro

```yaml
macros:
  - name: search_and_summarize
    description: "Search the web and summarize findings"
    params:
      query: { type: string, required: true }
      context: { type: string, default: "" }
    body:
      - id: search
        action: web_search.search_web
        params:
          query: "{{macro.query}}"
      - id: summarize
        agent: researcher
        input: |
          Summarize results for: "{{macro.query}}"
          Context: {{macro.context}}
          Results: {{result.search}}
```
