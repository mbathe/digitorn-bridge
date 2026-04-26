---
id: hook-events-reference
title: "Hook events reference"
type: hook-events
keywords: [hooks, events, turn_start, turn_end, tool_start, tool_end, session_start, session_end, pre_compact, error, approval_request, agent_spawn, agent_complete, activation]
---

# Hook events — reference

Every hook declares an `on:` event. When that event fires in the agent loop, the hook's condition is evaluated and, if it passes, the action executes. Events are semantic (not registered via a decorator); this page is the canonical list.

| Event | Aliases | Purpose |
|-------|---------|---------|
| `turn_start` | `user_prompt` | Fires at the beginning of each agent turn. Also triggered by the ``user_prompt`` alias. |
| `turn_end` | — | Fires at the end of each agent turn (after the final tool or reply for that turn). |
| `tool_start` | `pre_tool_use` | Fires right before a tool executes. Can gate, transform params, or inject messages. |
| `tool_end` | `post_tool_use` | Fires right after a tool returns. Can transform the result or inject a follow-up. |
| `session_start` | — | Fires at session creation (turn == 0). |
| `session_end` | — | Fires when ``manager.end_session`` closes the session. |
| `pre_compact` | — | Fires before the context-compaction step — ideal for custom compaction strategies. |
| `error` | — | Fires when the agent loop catches an exception (provider error, tool crash, etc.). |
| `approval_request` | — | Fires whenever ``ApprovalQueue.enqueue`` adds a new pending approval. |
| `agent_spawn` | — | Fires from the agent_spawn module when a sub-agent is launched. |
| `agent_complete` | — | Fires from the agent_spawn module when a sub-agent finishes. |
| `activation` | — | Declared-only event for background-trigger routing. Not fired by the runtime — the activation router consumes it. |

## YAML wiring

`on:` must be quoted as a string — YAML 1.1 treats `on`/`yes`/`no` as booleans.

```yaml compile=skip
execution:
  hooks:
    - id: my-hook
      "on": tool_start    # quote 'on' — YAML 1.1 truthiness
      condition:
        type: always
      action:
        type: log
        message: "Starting a tool"
```
