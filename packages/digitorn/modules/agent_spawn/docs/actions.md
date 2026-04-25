# Agent Spawn Actions

## LLM-exposed tools

- `Agent(prompt, description, specialist?, wait=true)` — spawn a sub-agent. Synchronous by default; set `wait=false` for background mode. For parallelism, call multiple `Agent` tools in the same turn.
- `AgentWaitAll(agent_ids?)` — collect results from background agents. Omit `agent_ids` to wait for all.

## Internal actions (not exposed to LLM)

These actions still exist for programmatic use (hooks, middleware) but are hidden from the LLM tool schema:

- `agent_status` — check progress of a running agent
- `agent_result` — get structured result of a completed agent
- `agent_list` — list all spawned agents
- `agent_wait` — block until a single agent finishes
- `agent_cancel` — cancel a running agent
- `reassign_agent` — restart an agent with a new task
