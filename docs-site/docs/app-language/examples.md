---

id: examples
title: Examples
sidebar_position: 16
format: md
---


# Examples

Complete, real-world application examples demonstrating different features of the LLMOS App Language.

## 1. Minimal Assistant

The simplest possible app — an LLM with file access.

```yaml
app:
  name: minimal-assistant
  version: "1.0"
  description: "Minimal AI assistant"

agent:
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
  system_prompt: "You are a helpful assistant. Workspace: {{workspace}}"
  tools:
    - module: filesystem
      action: read_file
    - module: filesystem
      action: list_directory

variables:
  workspace: "{{env.PWD}}"

triggers:
  - type: cli
    mode: conversation
```

## 2. Code Review Assistant

A single-agent app with macros, memory, and security.

```yaml
app:
  name: code-reviewer
  version: "2.0"
  description: "AI code review assistant"
  tags: [code-review, quality]

variables:
  workspace: "{{env.PWD}}"
  review_depth: thorough
  max_diff_lines: 2000

agent:
  id: reviewer
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
    temperature: 0.1
    max_tokens: 8192
  system_prompt: |
    You are a senior code reviewer.
    Workspace: {{workspace}}

    Severity levels: CRITICAL, HIGH, MEDIUM, LOW, INFO.
    Be constructive but thorough. Praise good code too.
  loop:
    type: reactive
    max_turns: 15
  tools:
    - module: filesystem
      action: read_file
    - module: filesystem
      action: list_directory
    - module: filesystem
      action: search_files
    - module: os_exec
      action: run_command

memory:
  conversation:
    max_history: 100
  episodic:
    auto_record: true
    auto_recall:
      on_start: true
      limit: 3

macros:
  - name: git_diff
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

  - name: run_linter
    params:
      tool: { type: string, required: true }
      args: { type: string, default: "." }
    body:
      - id: lint
        action: os_exec.run_command
        params:
          command: "{{macro.tool}} {{macro.args}}"
          working_directory: "{{workspace}}"

triggers:
  - type: cli
    mode: conversation
    greeting: |
      Code Reviewer v2.0 — Workspace: {{workspace}}
      Commands: "Review HEAD~1..HEAD", "Check src/main.py"
  - type: http
    path: /review

security:
  profile: power_user
  sandbox:
    allowed_paths: ["{{workspace}}"]
    blocked_commands: ["rm -rf", "git push", "git reset --hard"]
```

## 3. Multi-Agent Research Pipeline

Three agents with an explicit flow pipeline.

```yaml
app:
  name: research-pipeline
  version: "1.0"
  description: "Multi-agent research with structured flow"
  tags: [research, multi-agent]

variables:
  workspace: "{{env.PWD}}"
  output_dir: "{{workspace}}/research-output"

macros:
  - name: ensure_dir
    params:
      dir: { type: string, required: true }
    body:
      - action: os_exec.run_command
        params: { command: "mkdir -p {{macro.dir}}" }

agents:
  - id: planner
    role: coordinator
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514
      temperature: 0.3
      max_tokens: 2048
    system_prompt: "Break down questions into 3-5 subtasks. Output JSON array."
    tools: []

  - id: researcher
    role: specialist
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514
      temperature: 0.2
    system_prompt: "Summarize findings accurately. Cite sources."
    tools:
      - module: os_exec
        action: run_command

  - id: writer
    role: specialist
    brain:
      provider: anthropic
      model: claude-sonnet-4-20250514
      temperature: 0.4
      max_tokens: 8192
    system_prompt: "Write clear, well-structured reports in markdown."
    tools:
      - module: filesystem
        action: write_file

flow:
  - id: setup
    use: ensure_dir
    with: { dir: "{{output_dir}}" }

  - id: plan
    agent: planner
    input: "Break down: {{trigger.input}}"

  - id: research
    parallel:
      max_concurrent: 3
      fail_fast: false
      steps:
        - id: r1
          agent: researcher
          input: "Research overview: {{trigger.input}}"
        - id: r2
          agent: researcher
          input: "Research pitfalls: {{trigger.input}}"
        - id: r3
          agent: researcher
          input: "Find examples: {{trigger.input}}"

  - id: report
    agent: writer
    input: |
      Write research report.
      Question: {{trigger.input}}
      Overview: {{result.r1}}
      Pitfalls: {{result.r2}}
      Examples: {{result.r3}}

  - id: save
    action: filesystem.write_file
    params:
      path: "{{output_dir}}/report.md"
      content: "{{result.report}}"

triggers:
  - type: cli
    mode: one_shot
    greeting: "Enter your research question:"

security:
  profile: local_worker
  sandbox:
    allowed_paths: ["{{workspace}}"]
    blocked_commands: ["rm -rf", "curl", "wget"]
```

## 4. Full-Featured Coding Agent

Complete coding assistant with sub-agents, memory, context management, and macros.

```yaml
app:
  name: claude-code
  version: "4.0"
  description: "AI coding assistant with cognitive persistence and sub-agents"
  tags: [coding, assistant, multi-agent]

variables:
  workspace: "{{env.PWD}}"
  max_file_lines: 500
  shell_timeout: "30s"

agent:
  id: coder
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
    temperature: 0.2
    max_tokens: 8192
  system_prompt: |
    You are Claude Code, an AI coding assistant running inside LLMOS.

    ## Tools
    - filesystem: read_file, write_file, list_directory, search_files
    - os_exec: run_command
    - agent_spawn: spawn sub-agents for parallel work
    - context_manager: manage your context window
    - todo: persistent task tracking

    ## Guidelines
    - Read files before modifying
    - Make minimal, focused changes
    - Run tests after modifications
    - Never modify files outside {{workspace}}
    - Store important findings in memory

    Workspace: {{workspace}}
  loop:
    type: reactive
    max_turns: 30
  tools:
    # Filesystem
    - module: filesystem
      action: read_file
    - module: filesystem
      action: write_file
    - module: filesystem
      action: list_directory
    - module: filesystem
      action: search_files
    - module: filesystem
      action: create_directory
    - module: filesystem
      action: delete_file
    # Shell
    - module: os_exec
      action: run_command
    # Sub-agents
    - module: agent_spawn
      action: spawn_agent
    - module: agent_spawn
      action: check_agent
    - module: agent_spawn
      action: get_result
    - module: agent_spawn
      action: wait_agent
    - module: agent_spawn
      action: send_message
    - module: agent_spawn
      action: list_agents
    - module: agent_spawn
      action: cancel_agent
    # Context management
    - module: context_manager
      action: get_budget
    - module: context_manager
      action: compress_history
    - module: context_manager
      action: fetch_context
    - module: context_manager
      action: get_tools_summary
    - module: context_manager
      action: get_state

memory:
  conversation:
    max_history: 100
  working:
    max_size: "50MB"
  project:
    path: "{{workspace}}/.llmos/MEMORY.md"
    auto_inject: true
    agent_writable: true
    max_lines: 200
  episodic:
    auto_record: true
    auto_recall:
      on_start: true
      limit: 5

macros:
  - name: read_and_summarize
    params:
      path: { type: string, required: true }
      max_lines: { type: integer, default: 200 }
    body:
      - id: read_file
        action: filesystem.read_file
        params: { path: "{{macro.path}}" }
      - id: summarize
        agent: coder
        input: "Summarize (first {{macro.max_lines}} lines): {{result.read_file}}"

  - name: run_and_check
    params:
      command: { type: string, required: true }
      working_dir: { type: string, default: "{{workspace}}" }
    body:
      - id: exec
        action: os_exec.run_command
        params:
          command: "{{macro.command}}"
          working_directory: "{{macro.working_dir}}"
      - id: check
        branch:
          "on": "{{result.exec.exit_code}}"
          cases:
            "0":
              - agent: coder
                input: "Command succeeded. Output: {{result.exec.stdout}}"
          default:
            - agent: coder
              input: |
                Command failed (exit {{result.exec.exit_code}}).
                stderr: {{result.exec.stderr}}
                Diagnose and fix.

triggers:
  - type: cli
    mode: conversation
    greeting: |
      Claude Code v4.0
      Workspace: {{workspace}}
      Type your coding task. /clear to reset.
  - type: http
    path: /code

security:
  profile: power_user
  sandbox:
    allowed_paths: ["{{workspace}}"]
    blocked_commands: ["rm -rf /", "dd if=/dev/zero", "mkfs"]
```

## 5. Flow with Error Handling

Demonstrates try/catch, approval gates, and branching.

```yaml
app:
  name: deploy-pipeline
  version: "1.0"
  description: "Deployment pipeline with approval gates"

variables:
  workspace: "{{env.PWD}}"
  env: staging

agent:
  brain:
    provider: anthropic
    model: claude-sonnet-4-20250514
  system_prompt: "You manage deployments."
  tools:
    - module: os_exec
      action: run_command
    - module: filesystem

flow:
  # Step 1: Run tests
  - id: tests
    try:
      - action: os_exec.run_command
        params: { command: "pytest --tb=short" }
    catch:
      - error: "*"
        then: fail

  # Step 2: Build
  - id: build
    action: os_exec.run_command
    params: { command: "make build" }
    on_error: fail

  # Step 3: Approval gate
  - id: approve
    approval:
      message: "Deploy to {{env}}? Tests passed, build succeeded."
      options:
        - label: "Deploy"
          value: approve
        - label: "Cancel"
          value: reject
      timeout: "600s"
      on_timeout: reject
      "on":
        approve:
          goto: deploy
        reject:
          goto: cancelled

  # Step 4a: Deploy
  - id: deploy
    action: os_exec.run_command
    params: { command: "make deploy ENV={{env}}" }

  - id: done
    end:
      status: success
      output: { message: "Deployed to {{env}}" }

  # Step 4b: Cancelled
  - id: cancelled
    end:
      status: cancelled
      output: { message: "Deployment cancelled" }

triggers:
  - type: cli
    mode: one_shot

security:
  profile: power_user

capabilities:
  approval_required:
    - module: os_exec
      action: run_command
      when: "{{params.command | join(' ') | matches('deploy')}}"
      message: "Approve deployment command?"
  audit:
    level: full
```

## 6. Schedule-Triggered Monitor

A continuous monitoring app that runs on a schedule.

```yaml
app:
  name: system-monitor
  version: "1.0"
  description: "Scheduled system health monitor"

variables:
  workspace: "{{env.PWD}}"

agent:
  brain:
    provider: anthropic
    model: claude-haiku-4-5-20251001
    temperature: 0
    max_tokens: 2048
  system_prompt: |
    You monitor system health. Check disk, memory, and processes.
    Report issues concisely. Only alert on actual problems.
  tools:
    - module: os_exec
      action: run_command
    - module: filesystem
      action: write_file
  loop:
    type: single_shot
    max_turns: 5

triggers:
  - type: schedule
    cron: "*/30 * * * *"
    input: "Run system health check"

  - type: cli
    mode: one_shot
    greeting: "System Monitor — enter 'check' to run manually"

security:
  profile: readonly
```
