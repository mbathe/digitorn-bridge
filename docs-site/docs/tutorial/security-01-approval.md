---
id: security-01-approval
title: "Security 1 - Human-in-the-loop approval"
sidebar_label: "Security 1: Approval"
---

A capability `grant` says "the agent may call this without
asking". A capability `deny` says "never". Between the two sits
**`approve`**: the agent may try, but the call **pauses** until a
human (or a programmatic supervisor) authorises it. The pause is
synchronous - the agent loop blocks on the approval queue, the
turn waits, and the user sees a pending dialog in the client.

This is the default story for **shell access**, **destructive
operations**, **outbound network calls** in regulated apps. The
agent stays useful but can't act on its own when the consequences
matter.

## How it works

Three pieces compose:

- **`tools.capabilities.approve`** declares which actions need
  permission.
- **`approval_timeout`** (seconds, 30-3600, default 300) caps how
  long the daemon waits before auto-denying.
- **The approval queue** is exposed at
  `GET /api/apps/{app_id}/approvals` with a paired
  `POST /api/apps/{app_id}/approve` for resolution.

When an `approve`-policy action fires, the security gate
`gate4_policy` raises `ApprovalRequiredError`. The daemon
enqueues the request, the agent loop suspends, and clients
listening on the `/events` Socket.IO room see an
`approval_pending` event. A subsequent `approve` (or `deny`) call
unfreezes the loop and the agent resumes.

## The YAML

Save as `approval-bot.yaml`:

```yaml
app:
  app_id: approval-bot
  name: Approval Bot
  version: "1.0"

runtime:
  mode: conversation
  workdir_mode: auto
  max_turns: 6
  timeout: 120

agents:
  - id: main
    role: assistant
    brain:
      provider: deepseek
      model: deepseek-chat
      backend: openai_compat
      credential:
        ref: deepseek_main
        scope: per_user
        provider: deepseek
      config:
        api_key: "{{env.DEEPSEEK_API_KEY}}"
        base_url: https://api.deepseek.com/v1
      temperature: 0
      max_tokens: 256
    system_prompt: |
      You can run Bash commands. Be concise. If a command is
      approved and executes, summarise its output in one
      sentence.

tools:
  modules:
    shell: {}
  capabilities:
    default_policy: auto                     # other actions auto-allowed
    max_risk_level: high                     # accept high-risk actions
    approve:
      - module: shell
        actions: [bash]                      # every Bash call needs OK
        reason: "Shell commands need explicit approval before running."
    approval_timeout: 60                     # auto-deny after 60 s
```

The `approve` block is the only meaningful change. Everything else
is the standard chat scaffolding from the basic tutorials.

## Live transcript

Real session against the daemon. The user asks the agent to run
Bash; an SDK supervisor polls the approval queue and confirms.

### Step 1 - the agent attempts Bash

```text
> Run Bash to print "hello world".
```

The agent issues the tool call and the security gate intercepts:

```python
# captured by GET /api/apps/approval-bot/approvals
{
  "request_id": "81dc71da-b5d3-40e5-a49e-d11a441e882e",
  "agent_id": "main",
  "user_id": "e11e6e81e6864de9b654e02d309cc28a",
  "app_id": "approval-bot",
  "session_id": "test-c46d9f2a",
  "tool_name": "shell.bash",
  "tool_params": {
    "command": "echo \"hello world\"",
    "description": "Print hello world"
  },
  "risk_level": "high",
  "reason": "Shell commands need explicit approval before running."
}
```

The agent loop is **paused** on this request. No tokens get
billed, no further LLM call happens until the queue resolves.

### Step 2 - the supervisor approves

The Python testing SDK has a one-line helper:

```python
client.approve("approval-bot", "81dc71da-b5d3-40e5-a49e-d11a441e882e")
# → True
```

A web client posts the equivalent JSON to
`POST /api/apps/approval-bot/approve` with
`{"request_id": "...", "approved": true}`. Either way the
daemon unfreezes the agent loop.

### Step 3 - the action executes

```python
# captured tool_call event after approval
{
  "name": "Bash",
  "params": {"command": "echo \"hello world\"", "description": "Print hello world"},
  "success": true,
  "result": {"stdout": "hello world\n", "exit_code": 0}
}
```

Final agent reply:

```text
Printed "hello world" successfully.
```

`tool_calls_count: 1`, one Bash call, real output observed
end-to-end. The session went **user → pending approval →
human-in-the-loop OK → action runs → reply** with no other tool
calls.

## Denying instead of approving

```python
client.deny("approval-bot", request_id, reason="too risky in this session")
```

The agent receives a `permission_denied` error in place of the
tool result and the next turn picks an alternative path - or
gives up and tells the user it was blocked. Either response is
fine; the system prompt usually frames the fallback.

## Timeout behaviour

Set `approval_timeout` to bound the wait. The daemon emits an
`auto_denied` event once the deadline lapses; the agent receives
the same denial response it would have got from an explicit
`deny`. Useful for unattended sessions: a cron-driven agent that
hits an approve-only tool at 03:00 fails fast instead of holding
a worker forever.

The minimum is 30 s, the maximum is 3600 s. Lower is better for
interactive apps; higher is right for human-review flows where a
real reviewer is on shift.

## Mixing grant / approve / deny

The three policies compose by **action**. A typical production
shape:

```yaml
tools:
  capabilities:
    default_policy: block
    grant:
      - module: filesystem
        actions: [read, glob, grep]      # safe, auto-allowed
    approve:
      - module: filesystem
        actions: [write, edit]           # mutations need OK
      - module: shell
        actions: [bash]                  # any shell command needs OK
    deny:
      - module: filesystem
        actions: []                      # would deny everything
```

Resolution order is **deny > approve > grant > default_policy**.
The first match wins. An action listed in `deny` is unreachable
even if a `grant` row also names it. An action listed in
`approve` requires confirmation even if a wildcard `grant` would
have allowed it.

## When to use which

- **`grant`** for everything **read-only** (filesystem read,
  http get, list / browse / search).
- **`approve`** for everything that **mutates state the user
  cares about** (filesystem write/edit/delete, shell, http
  post/put/delete, network egress in sensitive apps).
- **`deny`** for everything **never legitimate** in this app
  (workspace.delete on a builder, http on an offline app,
  shell on a research-only assistant).

The `approve` flow is the difference between a polished agent
and a runaway one. Adding it to a single dangerous action makes
the agent feel cooperative; adding it to everything makes it
feel paralysed - calibrate.

## Going further

- The full security reference covering all seven gates:
  [Security architecture](../language/11-security.md).
- Programmatic approval workflows (channels module → approval
  queue, scheduled triggers + auto-approval logic):
  [Channels](../language/40-channels.md).
- The behaviour engine adds a different kind of guardrail
  (rule-based, not approval-based):
  [Advanced 4 - Behavior engine](advanced-04-behavior.md).
