---

id: flows
title: Flows
sidebar_position: 8
format: md
---


# Flows

Flows provide explicit control over execution order. When a `flow:` block is defined, it replaces the default agent loop with a structured pipeline.

## When to Use Flows

- **Without flow**: The agent loop runs autonomously - the LLM decides what to do
- **With flow**: You define the exact sequence of steps - deterministic orchestration

Use flows when you need predictable pipelines (CI/CD, data processing, multi-stage analysis). Use the agent loop for open-ended tasks (coding, research, conversation).

## Basic Flow

```yaml
flow:
  - id: read_config
    action: filesystem.read_file
    params:
      path: "{{workspace}}/config.json"

  - id: analyze
    agent: default
    input: |
      Analyze this configuration:
      {{result.read_config}}

  - id: save_report
    action: filesystem.write_file
    params:
      path: "{{workspace}}/report.md"
      content: "{{result.analyze}}"
```

## Flow Step Types

The LLMOS flow engine supports 18 step types.

### Action Step

Execute a module action directly:

```yaml
- id: get_files
  action: filesystem.list_directory
  params:
    path: "{{workspace}}/src"
  timeout: "10s"
  on_error: skip              # fail | skip | continue | rollback
  retry:
    max_attempts: 3
    backoff: exponential
  perception:                   # Optional: per-step perception override
    capture_before: true
    capture_after: true
    ocr_enabled: false
```

### Agent Step

Delegate to an LLM agent for reasoning:

```yaml
- id: think
  agent: planner                # Agent ID (or "default" for single agent)
  input: |
    Based on the file listing:
    {{result.get_files}}
    Decide which files need review.
```

### Sequence

Execute steps in order:

```yaml
- id: setup
  sequence:
    - action: os_exec.run_command
      params: { command: "mkdir -p output" }
    - action: os_exec.run_command
      params: { command: "git status" }
```

### Parallel

Execute steps concurrently:

```yaml
- id: checks
  parallel:
    max_concurrent: 3           # Max parallel tasks (default: 10)
    fail_fast: false             # Stop all on first failure
    steps:
      - id: lint
        action: os_exec.run_command
        params: { command: "ruff check ." }
      - id: typecheck
        action: os_exec.run_command
        params: { command: "mypy ." }
      - id: security
        action: os_exec.run_command
        params: { command: "bandit -r ." }
```

### Branch

Conditional execution based on an expression:

```yaml
- id: route
  branch:
    "on": "{{result.check.exit_code}}"
    cases:
      "0":
        - id: success
          agent: default
          input: "Tests passed! Summarize the results."
      "1":
        - id: fix
          agent: default
          input: "Tests failed. Analyze and fix: {{result.check.stderr}}"
    default:
      - id: unknown
        agent: default
        input: "Unexpected exit code: {{result.check.exit_code}}"
```

### Loop

Repeat steps until a condition is met:

```yaml
- id: retry_loop
  loop:
    max_iterations: 5
    until: "{{result.test.exit_code == 0}}"
    body:
      - id: fix_attempt
        agent: default
        input: "Fix the failing test. Attempt {{loop.iteration}}/5"
      - id: test
        action: os_exec.run_command
        params: { command: "pytest" }
```

The `loop` context provides:
- `{{loop.iteration}}` - Current iteration (0-based)
- `{{loop.item}}` - Current item (in map loops)

### Map

Apply steps to each item in a collection:

```yaml
- id: analyze_files
  map:
    over: "{{result.list_files}}"     # Expression yielding a list
    as: item                          # Variable name (default: "item")
    max_concurrent: 5                 # Parallel execution
    step:
      - id: analyze_one
        agent: default
        input: "Analyze file: {{loop.item}}"
```

### Reduce

Aggregate results from a collection:

```yaml
- id: combine
  reduce:
    over: "{{result.analyze_files}}"
    initial: { summary: "", count: 0 }
    as: acc
    step:
      agent: default
      input: |
        Current summary: {{loop.acc.summary}}
        New finding: {{loop.item}}
        Merge into updated summary.
```

### Race

Run steps in parallel, first to finish wins:

```yaml
- id: fastest
  race:
    steps:
      - id: search_web
        action: web_search.search
        params: { query: "{{topic}}" }
      - id: search_local
        action: filesystem.search_files
        params: { pattern: "{{topic}}" }
```

### Pipe

Chain steps where each step's output becomes the next step's input:

```yaml
- id: pipeline
  pipe:
    - action: filesystem.read_file
      params: { path: "{{workspace}}/data.json" }
    - agent: default
      input: "Parse and clean this data: {{result.previous}}"
    - action: filesystem.write_file
      params: { path: "{{workspace}}/clean.json", content: "{{result.previous}}" }
```

### Spawn

Spawn a sub-application:

```yaml
- id: sub_analysis
  spawn:
    app: "./analysis.app.yaml"       # Path to sub-app
    input: "Analyze {{workspace}}/src"
    timeout: "300s"
    await: true                      # Wait for result (default: true)
```

### Approval

Human approval gate:

```yaml
- id: deploy_approval
  approval:
    message: "Deploy to production?"
    options:
      - label: "Yes, deploy"
        value: approve
      - label: "No, cancel"
        value: reject
      - label: "Deploy with changes"
        value: modify
        schema:
          properties:
            changes: { type: string }
    timeout: "300s"
    on_timeout: reject
    channel: cli                     # cli | http | slack | email
    "on":
      approve:
        goto: deploy
      reject:
        goto: cancel
```

### Try/Catch

Error handling:

```yaml
- try:
    - action: os_exec.run_command
      params: { command: "risky-operation" }
  catch:
    - error: "*"                     # Catch all errors
      do:
        agent: default
        input: "Handle error: {{error}}"
      then: continue                 # fail | continue
  finally:
    - action: os_exec.run_command
      params: { command: "cleanup" }
```

### Dispatch

Dynamic module/action at runtime:

```yaml
- id: dynamic
  dispatch:
    module: "{{result.plan.module}}"
    action: "{{result.plan.action}}"
    params: "{{result.plan.params}}"
```

### Emit

Publish an event to the event bus:

```yaml
- id: notify
  emit:
    topic: "app.review.complete"
    event:
      status: "done"
      findings: "{{result.review.count}}"
```

### Wait

Wait for an event from the bus:

```yaml
- id: await_approval
  wait:
    topic: "app.approval.response"
    filter: "{{event.request_id == run.id}}"
    timeout: "3600s"
```

### End

Terminate the flow early:

```yaml
- id: abort
  end:
    status: failure                  # success | failure | cancelled
    output:
      error: "No files to process"
```

### Use Macro

Invoke a reusable macro (see [Macros](macros)):

```yaml
- id: check_code
  use: run_linter
  with:
    tool: "ruff check"
    args: "--output-format=text ."
```

### Goto

Jump to a labeled step:

```yaml
- id: retry
  goto: start                       # Jump to step with id "start"
```

## Referencing Step Results

Every step with an `id` stores its result. Access it with `{{result.step_id}}`:

```yaml
flow:
  - id: read_file
    action: filesystem.read_file
    params: { path: "config.json" }

  - id: process
    agent: default
    input: "Process: {{result.read_file}}"
```

For nested results, use dot notation: `{{result.step_id.field.subfield}}`.

## Flow with Checkpoint

Enable checkpoint to resume long-running flows after crashes or restarts. When `checkpoint: true`, the flow executor persists its state (completed steps + their results) to the KV store after each step. On restart, it loads the checkpoint and skips already-completed steps.

```yaml
app:
  name: etl-pipeline
  checkpoint: true          # Enable checkpoint/resume

flow:
  - id: step1
    action: filesystem.read_file
    params: { path: "data.txt" }

  # If the process crashes here, re-running resumes from step2
  # (step1's result is restored from the checkpoint)
  - id: step2
    agent: default
    input: "Process: {{result.step1}}"
```

### How It Works

1. **After each step** - The executor saves a checkpoint (completed step IDs, their results, and the next step index) to the KV store under the key `llmos:flow:checkpoint:<flow_id>`
2. **On restart with `resume=true`** - The executor loads the checkpoint, restores all completed step results into the expression context, and resumes from the next incomplete step
3. **On success** - The checkpoint is cleared
4. **On failure** - The checkpoint is **not** cleared, allowing you to fix the issue and retry from where it left off

### Requirements

- **KV store must be available** - Checkpointing requires a persistent KV store (SQLite-backed in daemon mode). Without a KV store, checkpoint is silently disabled.
- **Steps must have `id`** - Only steps with an `id` field have their results stored and restored. Anonymous steps are re-executed on resume.

### When to Use

- Long-running ETL pipelines
- Multi-stage deployments with approval gates
- Expensive LLM analysis flows where you don't want to re-process completed steps
