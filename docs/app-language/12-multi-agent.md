---
id: multi-agent
---

# Multi-Agent Systems

Digitorn supports multi-agent applications where a **coordinator agent** spawns isolated sub-agents that run in true parallelism. Each sub-agent has its own context window, memory, tools, and optionally its own LLM provider.

## Architecture

```
Coordinator (context window A)
|
|-- Agent(specialist="analyst", prompt="Analyze auth.py")  --> result B (synchronous)
|-- Agent(specialist="analyst", prompt="Analyze db.py")    --> result C (synchronous)
|-- Agent(prompt="Count all classes")                      --> result D (synchronous)
|
|   (when called in same turn, all three run in parallel)
|
'-- Coordinator aggregates results and produces final report
```

Key properties:
- **True parallelism** - sub-agents run as concurrent asyncio tasks
- **Total isolation** - each agent has its own context window, messages, and module instances
- **No shared memory** - agents can't see each other's state during execution
- **Structured results** - each agent returns findings, facts, errors, and todo state
- **Auto-notification** - the coordinator is notified when agents complete or fail

## YAML Configuration

### Minimal Example

```yaml
app:
  app_id: code-review
  name: "Code Review"

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    system_prompt: "You are a code review coordinator."
    pool:
      max_workers: 5

  - id: security_analyst
    role: specialist
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    specialty: "Security analysis -- finds vulnerabilities in code"
    system_prompt: "You are a security expert. Analyze code for vulnerabilities."
    modules: [filesystem, memory]

modules:
  filesystem:
    config:
      allowed_read: ["./"]
  memory:
    config:
      working_memory: true
      todo_list: true
```
### Full Configuration

```yaml
agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    system_prompt: "You coordinate the analysis."
    pool:
      max_workers: 5          # max concurrent agents (default: 3)
      progress: false         # relay sub-agent progress to coordinator (default: false)
      auto_retry: 0           # auto-retry failed agents (default: 0 = disabled)

  - id: code_analyst
    role: specialist
    brain:                    # can use a DIFFERENT model than coordinator
      provider: openrouter
      model: qwen/qwen3-235b-a22b
      config:
        api_key: "{{env.OPENROUTER_API_KEY}}"
        base_url: "https://openrouter.ai/api/v1"
    specialty: "Code analysis -- architecture, patterns, quality"
    # skills: "./skills/code_analysis.md"    # methodology file injected into system prompt
    system_prompt: "You are a code analyst."
    modules: [filesystem, memory]          # only these modules are available

  - id: security_analyst
    role: specialist
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    specialty: "Security analysis -- vulnerabilities, credentials, injection risks"
    # skills: "./skills/security_audit.md"
    system_prompt: "You are a security expert."
    modules: [filesystem, memory]
```
### Agent Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | **Yes** | Unique agent identifier |
| `role` | enum | **Yes** | `coordinator` or `specialist` |
| `brain` | object | **Yes** | LLM provider configuration |
| `system_prompt` | string | No | Agent instructions |
| `specialty` | string | No | One-line description of expertise (shown to coordinator) |
| `skills` | string | No | Path to a `.md` file with detailed methodology |
| `modules` | list | No | Module IDs the specialist can access (default: all) |
| `pool` | object | No | Coordinator-only: pool configuration |

### Pool Configuration (coordinator only)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pool.max_workers` | int | 3 | Maximum concurrent sub-agents |
| `pool.progress` | bool | false | Relay sub-agent progress events to coordinator |
| `pool.auto_retry` | int | 0 | Auto-retry failed/timed-out agents (0 = disabled) |

## Skills Files

A skills file is a Markdown document containing methodology, checklists, or domain knowledge. It is injected into the specialist's system prompt automatically.

Example `./skills/security_audit.md`:

```markdown
# Security Audit Methodology

When analyzing a Python file for security:

1. Check for hardcoded credentials (API keys, passwords, tokens)
2. Check for SQL injection (string concatenation in queries)
3. Check for command injection (subprocess with shell=True)
4. Check for path traversal (user input in file paths)
5. Check for insecure deserialization (pickle.loads, yaml.load)
6. Check environment variable handling (secrets in env)
7. Check error handling (stack traces exposed to users)
8. Check input validation and sanitization

Rate each finding: critical / high / medium / low / info
```

## Actions

The `agent_spawn` module exposes **2 tools** to the LLM, available directly to the coordinator (no discovery needed):

### Agent

Unified tool to spawn a sub-agent. **Synchronous by default** - blocks until the agent completes and returns its result. Set `wait=false` for background (fire-and-forget) execution.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `prompt` | string | **Yes** | - | Task description for the sub-agent |
| `description` | string | **Yes** | - | One-line description of what this agent does (for logging/UI) |
| `specialist` | string | No | null | ID of a specialist to use |
| `wait` | bool | No | true | Wait for agent to finish before returning |

**Two types of agents:**

```
# Specialist -- uses pre-configured brain, skills, modules (synchronous)
Agent(prompt="Analyze oauth.py for vulnerabilities", description="Security audit of oauth.py", specialist="security_analyst")

# Ad-hoc -- uses coordinator's brain (synchronous)
Agent(prompt="Count all Python classes", description="Class counter")

# Background -- returns immediately with agent_id
Agent(prompt="Deep analysis of auth module", description="Auth analysis", wait=false)
```

### AgentWaitAll

Collect results from background agents. If `agent_ids` is omitted, waits for ALL running agents.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `agent_ids` | list[string] | No | null | Specific agent IDs to wait for (null = all) |

```
AgentWaitAll()
--> [{agent_id: "agent_abc", status: "completed", content: "Found 2 vulns...", ...}, ...]

AgentWaitAll(agent_ids=["agent_abc", "agent_def"])
--> (returns results for only those two agents)
```

### Internal actions (not exposed to LLM)

The following actions still exist internally but are not shown to the LLM as tools:
- `agent_status` - check agent progress
- `agent_result` - get structured result of a completed agent
- `agent_list` - list all spawned agents
- `agent_wait` - block until a single agent finishes
- `agent_cancel` - cancel a running agent
- `reassign_agent` - reassign a failed agent with a new task

These can still be invoked programmatically via hooks or middleware.

## Execution Patterns

### Pattern 1: Parallel (same turn)

Call multiple Agent tools in the same turn. The LLM issues all calls at once, they run concurrently, and all results come back together.

```
Coordinator (single turn, multiple tool calls):
  Agent(specialist="analyst", prompt="Analyze file1.py", description="file1 analysis")
  Agent(specialist="analyst", prompt="Analyze file2.py", description="file2 analysis")
  Agent(specialist="analyst", prompt="Analyze file3.py", description="file3 analysis")
  --> All three results returned in the same turn
```

### Pattern 2: Sequential

Each Agent call is synchronous by default, so results flow naturally.

```
Coordinator:
  Agent(prompt="Research the topic", description="Research")  --> result with facts
  Agent(prompt="Write report using these facts: ...", description="Report writing")  --> final report
```

### Pattern 3: Background + Collect

Use `wait=false` for fire-and-forget, then `AgentWaitAll` to collect.

```
Coordinator:
  Agent(specialist="analyst", prompt="file1.py", description="file1", wait=false)  --> agent_001
  Agent(specialist="analyst", prompt="file2.py", description="file2", wait=false)  --> agent_002
  (continues working on other tasks...)

  AgentWaitAll()  --> [{agent_001 result}, {agent_002 result}]
  --> Aggregate all findings into report
```

## Isolation Model

Each sub-agent is fully isolated:

| Resource | Coordinator | Sub-Agent A | Sub-Agent B |
|----------|-------------|-------------|-------------|
| Context window | Own | Own | Own |
| Messages | Own | Own | Own |
| Memory (goal, todos, facts) | Own | Own | Own |
| Module instances | Own | Own (fresh) | Own (fresh) |
| LLM provider | Own | Own or shared | Own or shared |

Sub-agents cannot:
- Access the coordinator's memory or context
- Communicate with other sub-agents
- Spawn sub-sub-agents
- See tools outside their `modules` list

This isolation guarantees:
- No race conditions on module state
- No context window pollution
- True parallel execution with no locks or mutexes

## Notifications

The coordinator receives automatic notifications via the background notification system (same as watchers and scheduled jobs):

**Agent completed:**
```
[AGENT COMPLETED] agent_abc123 (security_analyst)
  Task: "Analyze oauth.py for vulnerabilities"
  Duration: 15.2s, 8 turns
  Findings: 3 facts stored
  Status: completed
```

**Agent failed:**
```
[AGENT FAILED] agent_def456
  Task: "Analyze module.py"
  Error: "Context overflow after reading 66KB file"
  Turns used: 5
  --> Retry with Agent(prompt="...", description="retry")
```

**Agent retrying (when auto_retry > 0):**
```
[AGENT RETRYING] agent_def456
  Attempt 2/2
  Reason: timeout
```

## Auto-Retry

When `pool.auto_retry` is set, failed or timed-out agents are automatically retried:

```yaml
pool:
  auto_retry: 1    # retry once on failure/timeout
```
- Only `timeout` and `failed` statuses trigger a retry
- `cancelled` agents are NOT retried
- The coordinator receives an `agent_retrying` notification
- After all retries are exhausted, the final status is reported

## Context Builder Integration

When specialists are defined, the context builder automatically injects information about the available agent pool into the coordinator's system prompt:

```
### Agent Pool

You have specialized agents at your disposal. They run in parallel
with their own context -- they don't consume your context window.

Available specialists:
  - security_analyst: Security analysis -- finds vulnerabilities in code
  - code_analyst: Code analysis -- architecture, patterns, quality

Use Agent(wait=true) for synchronous results, or Agent(wait=false) + AgentWaitAll
for background execution. Call multiple Agent tools in the same turn for parallelism.
Max parallel agents: 5
```

The coordinator is never forced to delegate -- it decides naturally based on the task.

## Complete Example

```yaml
app:
  app_id: multi-agent-audit
  name: "Multi-Agent Audit"

agents:
  - id: coordinator
    role: coordinator
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
      max_tokens: 4096
      context:
        max_tokens: 90000
        strategy: summarize
        keep_recent: 8
    system_prompt: |
      You are a senior software architect. You coordinate code audits
      by delegating file analysis to specialists and producing reports.
    pool:
      max_workers: 3
      auto_retry: 1

  - id: code_analyst
    role: specialist
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    specialty: "Code analysis -- architecture, patterns, quality assessment"
    # skills: "./skills/code_analysis.md"
    system_prompt: |
      You analyze Python source code for architecture patterns,
      code quality, and design issues. Be thorough and specific.
    modules: [filesystem, memory]

  - id: security_analyst
    role: specialist
    brain:
      provider: deepseek
      model: deepseek-chat
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: "https://api.deepseek.com/v1"
    specialty: "Security analysis -- vulnerabilities and risk assessment"
    # skills: "./skills/security_audit.md"
    system_prompt: |
      You analyze Python source code for security vulnerabilities.
      Rate findings as critical/high/medium/low/info.
    modules: [filesystem, memory]

modules:
  filesystem:
    config:
      allowed_read: ["./packages/"]
  memory:
    config:
      working_memory: true
      todo_list: true
      checkpoint: true

execution:
  workspace: "./packages/"
```