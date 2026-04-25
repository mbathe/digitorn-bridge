---

id: multi-agent
title: Multi-Agent
sidebar_position: 13
format: md
---


# Multi-Agent Systems

Multi-agent apps use multiple LLM agents, each with their own brain, tools, and system prompt. A runtime strategy coordinates their interaction.

## Defining Multiple Agents

Replace the `agent:` block with an `agents:` block containing a list of agents and a strategy:

```yaml
agents:
  strategy: pipeline                 # see Strategies below
  communication:
    mode: orchestrated               # see Communication Modes below
  agents:
    - id: planner
      role: coordinator
      brain:
        provider: anthropic
        model: claude-sonnet-4-6
        temperature: 0.3
      system_prompt: |
        You are a research planner. Break down questions into subtasks.
      tools: []

    - id: researcher
      role: specialist
      brain:
        provider: anthropic
        model: claude-sonnet-4-6
      system_prompt: |
        You are a research analyst. Summarize findings accurately.
      tools:
        - module: api_http
          actions: [http_request]

    - id: writer
      role: specialist
      brain:
        provider: anthropic
        model: claude-sonnet-4-6
        max_tokens: 8192
      system_prompt: |
        You are a technical writer. Create clear, structured reports.
      tools:
        - module: filesystem
          action: write_file
```

### Agent Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | **Yes** | Unique agent identifier (validated at compile time) |
| `role` | enum | No | `coordinator`, `specialist`, `reviewer`, `observer` |
| `expertise` | list | No | Areas of expertise (used by market strategy for bidding) |
| `preferred_node` | string | No | Preferred cluster node |
| `brain` | object | No | LLM configuration (provider, model, temperature, etc.) |
| `system_prompt` | string | No | Agent instructions |
| `tools` | list | No | Available tools |
| `loop` | object | No | Loop configuration |
| `signals` | list | No | Signal subscriptions |
| `watch` | object | No | Watch mode configuration |

### Agent Roles

| Role | Description |
|------|-------------|
| `coordinator` | Orchestrates other agents, delegates tasks, synthesizes results |
| `specialist` | Domain expert, focused on specific tools/tasks |
| `reviewer` | Reviews and validates output from other agents |
| `observer` | Monitors execution, logs metrics, doesn't intervene |

---


## Strategies

The `strategy` field determines how agents interact. Each strategy maps to a specialized runtime.

```yaml
agents:
  strategy: hierarchical   # hierarchical | pipeline | round_robin | consensus | adversarial | market
```

### Hierarchical (Default)

**Runtime:** `CentralizedRuntime`

The first agent acts as coordinator. It receives a `delegate` builtin tool to route tasks to other agents by ID.

```
Coordinator (planner)
    |-- delegate("researcher", "Research X") --> Researcher
    |-- delegate("writer", "Write report")  --> Writer
    '-- Return final synthesis
```

```yaml
agents:
  strategy: hierarchical
  agents:
    - id: planner
      role: coordinator
      tools:
        - builtin: delegate
      system_prompt: |
        Delegate tasks to specialists:
        - delegate(agent_id="researcher", task="...")
        - delegate(agent_id="writer", task="...")
    - id: researcher
      role: specialist
    - id: writer
      role: specialist
```

### Pipeline

**Runtime:** `PipelineRuntime`

Agents process sequentially. Each agent receives the previous agent's output as input. **Fail-fast:** if any agent fails, the pipeline stops immediately.

```
Input --> Agent A --> Agent B --> Agent C --> Output
         (draft)    (review)    (polish)
```

```yaml
agents:
  strategy: pipeline
  agents:
    - id: drafter
      system_prompt: "Write a first draft."
    - id: reviewer
      system_prompt: "Review and improve the draft."
    - id: editor
      system_prompt: "Polish the final version."
```

### Round Robin

**Runtime:** `CollaborativeRuntime`

Agents take turns processing the input. Each agent sees the previous agent's output as context, building iteratively.

```
Input --> Agent A --------> Agent B --------> Agent C --> Output
         (turn 1, sees     (turn 2, sees     (turn 3, sees
          original input)   A's output)       B's output)
```

```yaml
agents:
  strategy: round_robin
  agents:
    - id: analyst
      system_prompt: "Analyze the problem."
    - id: critic
      system_prompt: "Critique the analysis."
    - id: synthesizer
      system_prompt: "Synthesize the final answer."
```

### Consensus

**Runtime:** `CollaborativeRuntime`

All agents process the **same input in parallel** (`asyncio.gather`). Outputs are aggregated into a combined result.

```
         +-- Agent A --+
Input ---+-- Agent B --+--- Merge --> Output
         +-- Agent C --+
```

```yaml
agents:
  strategy: consensus
  agents:
    - id: optimist
      system_prompt: "Present the best-case scenario."
    - id: pessimist
      system_prompt: "Present the worst-case risks."
    - id: realist
      system_prompt: "Give a balanced, realistic assessment."
```

### Adversarial

**Runtime:** `AdversarialRuntime`

A proposer agent generates content and a critic agent evaluates it. They iterate in rounds until the critic approves (responds with `APPROVED` at the start) or max rounds are reached.

```
Round 1: Proposer --> "My proposal"
         Critic   --> "Needs improvement: ..."
Round 2: Proposer --> "Improved proposal" (with feedback)
         Critic   --> "APPROVED - looks great!"
```

```yaml
agents:
  strategy: adversarial
  adversarial:
    proposer: coder          # agent ID (validated at compile time)
    critic: reviewer         # agent ID (validated at compile time)
    max_rounds: 5            # 1-50, default: 5
  agents:
    - id: coder
      role: specialist
      system_prompt: |
        Write clean, correct code. When you receive feedback,
        improve your code accordingly.
    - id: reviewer
      role: reviewer
      system_prompt: |
        Evaluate the proposed code. If production-ready, respond
        with "APPROVED" at the start. Otherwise, provide specific
        feedback for improvement.
```

**Configuration:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `adversarial.proposer` | string | 1st agent | ID of the proposer agent |
| `adversarial.critic` | string | 2nd agent | ID of the critic agent |
| `adversarial.max_rounds` | int | 5 | Maximum iteration rounds (1-50) |
| `adversarial.termination` | string | "" | Custom termination expression (future) |

### Market

**Runtime:** `MarketRuntime`

Tasks are broadcast to all agents as an auction. Each agent submits a bid (confidence score + reasoning). The highest-confidence bidder wins and executes the task.

```
Phase 1 - Bidding:
  Task broadcast --> Agent A bids 0.3
                     Agent B bids 0.95  <-- Winner
                     Agent C passes

Phase 2 - Execution:
  Agent B executes the full task --> Output
```

```yaml
agents:
  strategy: market
  market:
    allow_pass: true         # agents can decline tasks outside their expertise
    max_bid_rounds: 1        # bidding rounds per task
  agents:
    - id: python_expert
      role: specialist
      expertise: [python, testing, debugging]
      system_prompt: "You are a Python expert."
    - id: frontend_dev
      role: specialist
      expertise: [react, css, typescript]
      system_prompt: "You are a frontend developer."
    - id: devops
      role: specialist
      expertise: [docker, kubernetes, ci-cd]
      system_prompt: "You are a DevOps engineer."
```

**Configuration:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `market.allow_pass` | bool | true | Allow agents to decline tasks |
| `market.max_bid_rounds` | int | 1 | Number of bidding rounds (1-5) |
| `market.auctioneer` | string | "" | Optional coordinator agent ID |

**Bid format:** Agents respond with JSON:
```json
{
  "bid": true,
  "confidence": 0.95,
  "reasoning": "This is a Python testing task, my specialty",
  "approach": "Write pytest fixtures with parametrize"
}
```

---


## Communication Modes

Communication mode **overrides** the strategy dispatch. When set to `peer_to_peer` or `blackboard`, the strategy field is ignored.

```yaml
agents:
  strategy: consensus              # ignored when mode != orchestrated
  communication:
    mode: peer_to_peer             # orchestrated | peer_to_peer | blackboard
    topology: ring                 # mesh | ring | star (P2P only)
```

| Mode | Description | Overrides Strategy? |
|------|-------------|---------------------|
| `orchestrated` | Default. Strategy controls agent coordination | No |
| `peer_to_peer` | Agents communicate directly via message queues | **Yes** |
| `blackboard` | Agents share a structured state in rounds | **Yes** |

### Peer-to-Peer

**Runtime:** `PeerToPeerRuntime`

All agents run **concurrently**. Each agent gets a `send_message` builtin tool routed through a `ChannelManager` that enforces topology constraints.

```yaml
agents:
  communication:
    mode: peer_to_peer
    topology: ring                   # mesh | ring | star
  agents:
    - id: researcher
      tools:
        - builtin: send_message
      system_prompt: |
        Research the topic. Send findings to the analyst:
        send_message(target_id="analyst", message="My findings: ...")

    - id: analyst
      tools:
        - builtin: send_message
      system_prompt: |
        Analyze incoming research. Forward to the writer.

    - id: writer
      tools:
        - builtin: send_message
        - module: filesystem
          action: write_file
      system_prompt: |
        Write the final report from the analysis.
```

**Topologies:**

| Topology | Routing | Use Case |
|----------|---------|----------|
| `mesh` | Every agent can message every other agent | General collaboration |
| `ring` | Each agent can only message the next agent | Sequential workflows |
| `star` | Hub (1st agent) talks to all; spokes only talk to hub | Centralized review |

```
mesh:                ring:                star:
A <--> B             A --> B              Hub <--> Spoke1
A <--> C             B --> C              Hub <--> Spoke2
B <--> C             C --> A              Hub <--> Spoke3
```

> **Note:** Topology is validated at compile time. Invalid topologies produce a warning and default to `mesh`.

### Blackboard

**Runtime:** `BlackboardRuntime`

Agents share a structured state (`SharedState`) and take turns reading/writing to it. Continues until all agents succeed in a round or max rounds (3) are reached.

```yaml
agents:
  communication:
    mode: blackboard
  agents:
    - id: architect
      system_prompt: |
        Read the blackboard and contribute architectural decisions.
    - id: security_expert
      system_prompt: |
        Read the blackboard and identify security concerns.
    - id: devops
      system_prompt: |
        Plan infrastructure based on the architecture and security requirements.
```

**How it works:**

1. A `SharedState` is created with `{task, status, contributions}`
2. Each round, every agent reads the current state via `format_context()`
3. Agent processes, produces output
4. Output is recorded via `update(agent_id, round, output, success)`
5. If all agents succeed in a round, state is marked complete
6. Final output combines all contributions: `[agent_id]: output`

---


## Using Agents in Flows

In multi-agent apps with explicit `flow:` blocks, reference agents by their ID in `agent` steps:

```yaml
agents:
  agents:
    - id: planner
      system_prompt: "Break down tasks."
    - id: researcher
      system_prompt: "Research topics."
    - id: writer
      system_prompt: "Write reports."

flow:
  - id: plan
    agent: planner
    input: "Break down: {{trigger.input}}"

  - id: research
    parallel:
      steps:
        - id: r1
          agent: researcher
          input: "Research overview: {{trigger.input}}"
        - id: r2
          agent: researcher
          input: "Research risks: {{trigger.input}}"

  - id: report
    agent: writer
    input: |
      Write report from:
      Overview: {{result.r1}}
      Risks: {{result.r2}}

  - id: save
    action: filesystem.write_file
    params:
      path: "{{variables.output_dir}}/report.md"
      content: "{{result.report}}"
```

## Delegation

In the agent loop (without explicit flows), the coordinator can delegate to other agents using the `delegate` builtin:

```yaml
agents:
  strategy: hierarchical
  agents:
    - id: lead
      role: coordinator
      tools:
        - builtin: delegate
      system_prompt: |
        Delegate tasks to specialists:
        - delegate(agent_id="researcher", task="Research X")
        - delegate(agent_id="writer", task="Write report on X")
    - id: researcher
      role: specialist
    - id: writer
      role: specialist
```

## Sub-Agent Spawning

For single-agent apps, use the `agent_spawn` module to create autonomous sub-agents at runtime:

```yaml
agent:
  tools:
    - module: agent_spawn
      actions: [spawn_agent, wait_agent, get_result, send_message]
  signals:
    - on: agent.completed
      inject: |
        [SIGNAL] Agent "{{signal.agent_name}}" completed.
        Use get_result("{{signal.spawn_id}}") for full output.
  watch:
    timeout: 600s
  system_prompt: |
    Spawn sub-agents for parallel work:
    - spawn_agent(name="analyzer", objective="...", tools=["filesystem.read_file"])
    - wait_agent(spawn_id="...")
    - get_result(spawn_id="...")
```

Spawned agents inherit the parent's brain config and run independently with their own LLM loop.

---


## Strategy Reference

| Strategy | Runtime | Agents | Communication | Use Case |
|----------|---------|--------|---------------|----------|
| `hierarchical` | `CentralizedRuntime` | 1 coordinator + N workers | delegate builtin | Boss delegates tasks |
| `pipeline` | `PipelineRuntime` | N sequential | Pass output forward | Draft -> review -> polish |
| `round_robin` | `CollaborativeRuntime` | N sequential | Context forwarding | Iterative refinement |
| `consensus` | `CollaborativeRuntime` | N parallel | Aggregate outputs | Diverse perspectives |
| `adversarial` | `AdversarialRuntime` | 2 (proposer + critic) | Iterative feedback | Code review, validation |
| `market` | `MarketRuntime` | N bidders, 1 winner | Auction/bid JSON | Dynamic task allocation |

| Communication | Runtime | Overrides Strategy | Topology |
|---------------|---------|-------------------|----------|
| `orchestrated` | (per strategy) | No | N/A |
| `peer_to_peer` | `PeerToPeerRuntime` | **Yes** | mesh, ring, star |
| `blackboard` | `BlackboardRuntime` | **Yes** | N/A |

## Compile-Time Validation

The compiler validates multi-agent configurations:

- Every agent must have a unique `id`
- `adversarial.proposer` and `adversarial.critic` must reference existing agent IDs
- `adversarial` strategy requires at least 2 agents
- `market.auctioneer` must reference an existing agent ID (if specified)
- P2P and blackboard modes require at least 2 agents
- Unknown topologies produce a warning (default: `mesh`)
- Missing `send_message` builtin in P2P mode produces an info (auto-injected at runtime)
