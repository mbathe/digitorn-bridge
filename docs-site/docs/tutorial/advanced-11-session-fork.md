---
id: advanced-11-fork
title: "Advanced 11 - Session forking"
sidebar_label: "Advanced 11: Session fork"
---

A Digitorn session is a **stateful conversation thread**: turn
history, memory, working facts, workspace contents. Most apps
have one session per user per app. Some apps need to **branch
the conversation** - what would happen if the agent had
answered differently? what if the user picked option B instead
of A? what does the next 5 turns look like from this point
forward?

**Session forking** is the primitive. The daemon clones an
existing session at its current state, gives you a new
session_id, and lets you continue both branches independently.
The fork inherits the parent's full message history, memory
state, and workspace; new turns on either side don't affect
the other.

## When you'd use it

- **A/B testing prompts**. Fork after the system prompt loads,
  send the same user message to both branches with different
  follow-ups, compare outputs.
- **What-if exploration**. The user is about to make a decision
  ("delete this folder?"). Fork, run the destructive action on
  the fork, show the user the outcome, then either commit
  (delete on the original too) or discard (drop the fork).
- **Time-travel debugging**. Fork from a session that produced
  a buggy answer; replay with different tool params or a
  different model to find what would have worked.
- **Parallel exploration**. Fork into 5 branches, each
  exploring a different sub-question, merge the conclusions.
- **Per-user customisation of a shared template**. Fork a
  "demo" session into per-user copies; everyone starts with
  the same context but evolves independently.

## The endpoint

```text
POST /api/apps/{app_id}/sessions/{source_session_id}/fork
```

Through the Python testing SDK:

```python
fork = client.fork_session(session)
# → {
#     "session_id": "<uuid>",
#     "source_session_id": "test-...",
#     "forked_from": "test-...",
#     "new_session_id": "<uuid>",
#     "forked": True,
#     "message_count": 3,
#   }
```

The new `session_id` is a fresh UUID (no `test-` prefix; it's
not derived from the parent). The fork is **independent** from
the moment of creation - changes to the parent don't propagate
to the fork and vice versa.

## Live transcript

Real session captured against the daemon. The setup uses
`memory-bot` from [tutorial 2](02-conversation-with-memory.md):
an agent with the memory module, configured to remember facts
the user shares.

### Step 1 - plant a fact in the original session

```text
> My favorite color is purple. Just acknowledge with one word.

Noted.
```

The agent quietly called `Remember(content="User's favorite
color is purple")` before replying.

### Step 2 - fork

```python
client.fork_session(session)
# returned:
# {
#   "session_id": "d05d9166-7adb-4f53-b147-cdee07c5a282",
#   "source_session_id": "test-94a8135d",
#   "forked": True,
#   "message_count": 3
# }
```

`message_count: 3` = the system prompt + the one user message
+ the one assistant reply that existed at fork time.

### Step 3 - the fork remembers

```text
[forked session]
> What's my favorite color?

Purple.
```

The fork inherited the memory facts from the parent. The agent
either had the colour fact still in working context (likely,
since the parent only had one turn) or recalled it via memory.
Either path produced the right answer.

### Step 4 - the original is unchanged

```text
[original session]
> What's my favorite color?

Purple.
```

The original session also answers correctly - it never
"shared" anything with the fork; it just kept its own state
intact while the fork went off independently.

If the original had received a different message between
steps 2 and 4 (say "actually I changed my mind, my favorite
is green"), the original would now answer "Green" and the
fork would still answer "Purple". The two branches diverge
the moment the fork happens.

## What gets forked

The fork copies **all** session-scoped state:

- Message history (every turn so far)
- Memory state (goal, todos, facts)
- Workspace contents (every file the agent wrote)
- Per-session widget state
- Per-session approval queue (resolved approvals stay; pending
  approvals are dropped because they belong to the parent's
  paused agent loop)
- Token / cost counters reset to zero in the fork

What does **not** get copied: per-app shared state (the rag
knowledge base, the shared queue, channel subscriptions). Two
forks of the same parent see the same shared state, but
neither sees the other's changes to it.

## Compose with other primitives

Forks pair well with several other primitives.

**With the dev CLI**, fork a saved session for replay-style
experimentation: `digitorn dev chat <app> --resume <fork_id>`
picks up the conversation from the fork point.

**With behaviour rules**, set a custom rule that auto-forks a
session right before a destructive operation:

```yaml
security:
  behavior:
    rule_definitions:
      - id: fork_before_delete
        trigger: [delete]
        when: pre_tool
        action: warn                 # tool runs but message landed first
        message: "About to delete - the previous state is in fork {{...}}"
```

(The `{{...}}` placeholder isn't actually populated; the
example shows the **intent**. Real implementation needs a hook
chain.)

**With composition**, a coordinator app can fork a target app's
session before each `call_app` invocation, giving each call its
own clean branch off a warm baseline.

## Persistence

Forks are **persisted to the database** alongside their
parents. They show up in `list_sessions` for the same app and
the same user. Cleanup follows the same retention policy as
any session - the daemon's
[`session_events_retention_days`](../language/23-configuration.md)
applies. There's no automatic cleanup of old forks; they're
just sessions like any other.

For test scaffolding, the SDK's `delete_session(handle)` removes
a fork (or any session) cleanly, including its event log and
memory state.

## Going further

- The full session API surface (create, send, fork, compact,
  abort, resume, delete):
  [API integration](../language/14-api-integration.md).
- The companion **`compact_session`** call that runs context
  compaction on a session without forking - useful when a
  long thread needs to shrink in place:
  [Context management](../language/06-context-management.md).
- The persistent event log a fork inherits from its parent:
  [Security 7 - Audit log](security-07-audit.md).
