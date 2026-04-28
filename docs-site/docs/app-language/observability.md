---

id: observability
title: Observability
sidebar_position: 14
format: md
---


# Observability

Configure streaming, logging, tracing, and metrics for your app.

## Configuration

```yaml
observability:
  streaming:
    enabled: true
    channels: [cli, sse]
    include_thoughts: true
    include_tool_calls: true
    include_results: false

  logging:
    level: info                    # debug | info | warning | error
    format: structured             # structured | plain
    file: ""                       # Log file path (empty = stdout only)

  tracing:
    enabled: false
    backend: opentelemetry
    sample_rate: 1.0               # 0.0-1.0

  metrics:
    - name: tool_calls
      type: counter
      track: "{{agent.tool_call_count}}"
    - name: response_time
      type: histogram
      track: "{{agent.last_response_ms}}"
```

## Streaming

Real-time output streaming during agent execution.

```yaml
observability:
  streaming:
    enabled: true                  # Enable streaming (default: true)
    channels:
      - cli                        # Terminal output
      - sse                        # Server-Sent Events
    include_thoughts: true         # Stream LLM reasoning
    include_tool_calls: true       # Stream tool invocations
    include_results: false         # Stream tool results
```

### Channels

| Channel | Description |
|---------|-------------|
| `cli` | Print to terminal (for `llmos app run`) |
| `sse` | Server-Sent Events (for HTTP trigger) |

### SSE Events

When streaming via SSE, the client receives events like:

```
event: thought
data: {"content": "I'll read the file first..."}

event: tool_call
data: {"module": "filesystem", "action": "read_file", "params": {"path": "/src/main.py"}}

event: tool_result
data: {"result": "file contents..."}

event: response
data: {"content": "Here's what I found..."}

event: done
data: {"success": true}
```

## Logging

```yaml
observability:
  logging:
    level: info
    format: structured
    file: "{{workspace}}/logs/app.log"
```

### Log Levels

| Level | Description |
|-------|-------------|
| `debug` | All internal details |
| `info` | Normal operations (default) |
| `warning` | Potential issues |
| `error` | Errors only |

### Log Formats

| Format | Description |
|--------|-------------|
| `structured` | JSON-formatted structured logs (default) |
| `plain` | Human-readable plain text |

## Tracing

Distributed tracing for complex multi-agent or multi-step flows.

```yaml
observability:
  tracing:
    enabled: true
    backend: opentelemetry
    sample_rate: 1.0
```

When enabled, tracing is **deeply integrated** into all runtime paths:

- **`run()` / `_run_core()`** - Root span wraps the entire app execution
- **`run_flow()`** - Root span wraps flow execution; each tool call gets a child span
- **`run_multi_agent()`** - Root span includes strategy and agent count
- **`stream()` / `_stream_with_history()`** - Root span wraps streaming execution

Every tool call creates a child span with module, action, duration, and error status. Spans are emitted to the EventBus (`llmos.tracing` topic) for real-time monitoring.

### Sampling

Control trace volume with `sample_rate`:

```yaml
observability:
  tracing:
    enabled: true
    sample_rate: 0.1    # Only trace 10% of requests
```

A rate of `1.0` traces everything (useful for development). A rate of `0.0` disables tracing entirely.

## Custom Metrics

Define custom metrics to track during execution:

```yaml
observability:
  metrics:
    - name: files_processed
      type: counter
      track: "{{result.process.file_count}}"

    - name: analysis_quality
      type: gauge
      track: "{{result.review.quality_score}}"

    - name: llm_latency
      type: histogram
      track: "{{agent.last_response_ms}}"
```

### Metric Types

| Type | Description |
|------|-------------|
| `counter` | Monotonically increasing count |
| `gauge` | Current value (can go up or down) |
| `histogram` | Distribution of values |

### Built-in Track Expressions

In addition to custom expressions, the runtime automatically tracks these for every tool call:

| Expression            | Type      | Description                         |
|-----------------------|-----------|-------------------------------------|
| `action.duration_ms`  | histogram | Tool call duration in milliseconds  |
| `action.count`        | counter   | Total tool calls                    |
| `action.error`        | counter   | Failed tool calls                   |
| `action.success`      | counter   | Successful tool calls               |
| `action.tokens`       | counter   | Token usage per call                |

### Metrics + EventBus

When an EventBus is configured (daemon mode), metrics are emitted to the `llmos.metrics` topic. This enables real-time dashboards and alerting.

### Compiler Validation

The compiler validates metric definitions at compile time:

- Unknown `type` values generate a warning (valid: `counter`, `gauge`, `histogram`)
- Unknown `track` expression prefixes generate a warning

## Perception (Screenshot/OCR)

The `perception:` block enables automatic screenshot and OCR capture around tool calls. This is useful for desktop automation, visual verification, and debugging GUI workflows.

```yaml
perception:
  enabled: true                    # Enable perception capture (default: false)
  capture_before: false            # Screenshot before tool execution
  capture_after: true              # Screenshot after tool execution (default: true)
  ocr_enabled: false               # Run OCR on screenshots
  timeout_seconds: 10              # Capture timeout

  # Per-action overrides
  actions:
    gui.click_position:
      capture_before: true         # Always capture before clicks
      capture_after: true
      ocr_enabled: true            # OCR after GUI clicks
    browser.navigate_to:
      capture_after: true
      ocr_enabled: false
```

### Perception Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable perception globally |
| `capture_before` | bool | `false` | Screenshot before each tool call |
| `capture_after` | bool | `true` | Screenshot after each tool call |
| `ocr_enabled` | bool | `false` | Run OCR on captured screenshots |
| `timeout_seconds` | int | `10` | Timeout for capture operations |
| `actions` | dict | `{}` | Per-action overrides keyed by `module.action` |

### Per-Action Overrides

Override perception settings for specific module actions:

```yaml
perception:
  enabled: true
  capture_after: true              # Default: capture after all actions

  actions:
    # Only capture before/after GUI clicks, with OCR
    gui.click_position:
      capture_before: true
      capture_after: true
      ocr_enabled: true
      timeout_seconds: 15

    # Skip capture for file reads (too noisy)
    filesystem.read_file:
      capture_before: false
      capture_after: false
```

Each per-action override field:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `capture_before` | bool | `false` | Screenshot before this action |
| `capture_after` | bool | `true` | Screenshot after this action |
| `ocr_enabled` | bool | `false` | Run OCR on screenshots |
| `validate_output` | string | `""` | JSONPath expression to validate output |
| `timeout_seconds` | int | `10` | Capture timeout |

### When to Use Perception

- **Desktop automation** - Verify GUI state between clicks
- **Browser automation** - Capture page state for debugging
- **Visual QA** - OCR to verify text content on screen
- **Audit trail** - Screenshot evidence of actions taken

> **Note**: Perception requires the `perception_vision` module (daemon mode). In standalone mode, perception config is stored but not executed.

## Audit Logging

Audit logging is configured in the `capabilities:` block:

```yaml
capabilities:
  audit:
    level: full
    log_params: true
    redact_secrets: true
    notify_on:
      - event: error
        channel: log
      - event: security_violation
        channel: log
```

See [Security](security) for details.

## Debugging Tips

### 1. Enable debug logging

```yaml
observability:
  logging:
    level: debug
```

### 2. Watch streaming output

```bash
llmos app run my-app.app.yaml
# Streaming output shows each thought, tool call, and result in real-time
```

### 3. Validate before running

```bash
llmos app validate my-app.app.yaml
```

### 4. Check context budget

Add context_manager tools to see token usage:

```yaml
agent:
  tools:
    - module: context_manager
      action: get_budget
    - module: context_manager
      action: get_state
```

### 5. Use the todo builtin

Track agent progress with the persistent todo list:

```yaml
# The agent automatically has access to:
# todo(action="add", task="...")
# todo(action="list")
```
