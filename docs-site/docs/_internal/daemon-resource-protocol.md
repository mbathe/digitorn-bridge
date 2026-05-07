# Daemon Resource Protocol - E2E manual test guide

This guide walks through 5 scenarios that exercise the full
synchronisation contract end-to-end. Run them after a fresh daemon
build + a fresh client rebuild. They reproduce each of the 4 zombie
bugs the protocol was designed to eliminate, and verify the
reconciliation primitives behave as designed.

## Pre-flight

1. Daemon: `digitorn daemon start` (or your usual launcher). Confirm
   the new boot log line appears: look for `instance_id=<32-hex>` on
   stdout, OR open any `/api/...` endpoint in the browser dev tools
   and check that the response carries an `X-Digitorn-Instance:
   <hex>` header. Same value should match the `instance_id` field
   on the Socket.IO `connected` event payload.

2. Web client: `cd digitorn_web && npm run dev`. Open the chat in
   one tab. Open dev tools → Network → WS, click the
   `/socket.io/?...` connection, switch to the Messages panel.
   Filter for `type:"heartbeat"` - you should see one every 5 s
   while a session is open.

3. Flutter client: `cd digitorn_client && flutter run`. Open the
   chat. Logs should show `socket→any heartbeat` every 5 s.

If any of those check fail, **stop here** - the daemon protocol
isn't live yet.

## Scenario 1 - Spinner zombie (was the #1 bug)

**Setup**: open chat, send a message that triggers a long-running
agent action (e.g. `Run a multi-step task that takes 30 s`).

**Action**: while the turn is running, on the daemon side, simulate
the chain of events that caused the original bug:

```bash
# Find the daemon PID
ps aux | grep digitorn-daemon

# Send SIGTERM (graceful) - daemon emits ``message_failed`` +
# ``turn_terminal`` then exits cleanly
kill -TERM <pid>
```

**Expected**:

- The send-button spinner stops within ~1 s of the kill (not after
  the 25 s watchdog timeout).
- Connection badge transitions: `live` → `reconnecting` → `stale`
  → (after restart) `restarted` → `live`.
- The chat shows the assistant bubble closed with an error /
  cancelled marker, no zombie pulsing dot.
- The send button re-enables once the badge is back to `live`.

**Failure**: spinner stays spinning past 30 s, OR the badge never
shows `restarted`, OR clicking the send button mid-stale somehow
manages to fire a request.

## Scenario 2 - Background task zombie

**Setup**: ask the agent `Run "sleep 30 && echo done" in the
background`. The agent calls `Bash(run_in_background=true)` and
returns a `task_id`.

**Action**: kill the daemon (`kill -9`) **before** the 30 s sleep
finishes.

**Expected**:

- The TasksPanel chip shows the task's status flip from `running`
  → `cancelled` within ~2 s of the daemon dying. Reason field on
  the task: "turn ended".
- After the daemon restarts, opening a fresh session shows zero
  zombie tasks lingering from the previous run.

**Failure**: the task stays `running` forever in the panel.

## Scenario 3 - Sub-agent animation zombie

**Setup**: ask the coordinator `Spawn 3 explorer sub-agents in
parallel and wait for them` (or any prompt that triggers
`Agent(prompt=…)`).

**Action**: while the 3 sub-agents are pulsing, force an error on
the coordinator side. Easiest way: from the daemon shell, send
`POST /api/apps/<app>/sessions/<sid>/abort`.

**Expected**:

- The coordinator's send-button spinner stops.
- All 3 sub-agent dots transition from pulsing to static (✓ or ✗).
- The agent_group bubble shows them as `cancelled` with reason
  "turn ended".
- A `turn_terminal` event with `status: "cancelled"` appears in
  the dev-tools WS feed.

**Failure**: even one of the 3 dots keeps pulsing, OR the bubble
shows mixed states.

## Scenario 4 - Tab inactive → focus reconcile

**Setup**: open the chat, send a quick message, wait for the turn
to end.

**Action**: switch to another tab for 90 s without doing anything
on the chat tab. While away, send another message via the
browser's curl panel:

```bash
curl -X POST http://localhost:8000/api/apps/<app>/sessions/<sid>/messages \
  -H "Authorization: Bearer <token>" \
  -d '{"message":"hi from curl"}'
```

Then switch back to the chat tab.

**Expected**:

- On focus, the daemon-resource protocol fires a snapshot
  reconcile (visible in dev tools Network as
  `GET /sessions/<sid>/snapshot?since=<lastSeq>`).
- The chat shows the curl-sent message + the assistant's reply
  even though the tab was idle when they arrived.
- The connection badge stays `live` (no jarring `stale` → `live`
  flicker if the tab comes back fast enough).

**Failure**: the chat stays frozen at its pre-blur state and you
must reload to see the new message.

## Scenario 5 - Daemon socket blip (transient drop)

**Setup**: open the chat, idle.

**Action**: from the OS network panel, briefly cut the daemon's
socket binding (e.g. block port 8000 for 8 s via `iptables`,
Windows firewall, or just restart the daemon's socket adapter
without restarting the process - easiest: pause the daemon with
`kill -STOP` then `kill -CONT` after 8 s).

**Expected**:

- Badge: `live` → `reconnecting` (immediately) → after 15 s →
  `stale` IF the pause exceeds the heartbeat threshold.
- On `kill -CONT`: badge → `reconnecting` → `live`.
- No zombie state - the chat resumes from where it left off, no
  reload needed.
- `instance_id` is the SAME before and after the blip (no
  `restarted` flash) because the process never died.

**Failure**: the badge stays stuck at `reconnecting` past 30 s
even after CONT, OR an instance mismatch is detected for a process
that didn't actually restart.

## Scenario 6 - Late event arrives after turn_terminal

This one is harder to reproduce naturally; here's the cheat:

**Setup**: open dev tools → Network → WS. Pin a recent
`agent_event` payload from a turn that completed. Note its
`correlation_id` and `seq`.

**Action**: replay that payload through Socket.IO by emitting a
fake event from the client:

```js
window.__socket.emit("event", { /* paste payload, but bump seq below the watermark */ });
```

**Expected**:

- The agent's status in the workspace panel does NOT revert to
  `running`. The `lastTerminalSeq` guard drops the late event
  silently.
- A console.debug line shows the event was suppressed (only in
  dev mode).

**Failure**: the agent's pulsing dot reappears even though its
turn ended.

## What to do if a scenario fails

1. Capture the daemon logs from `kill` to next `live` (greppable
   for `instance_id=`, `seq=`, `turn_terminal`).
2. Capture the WS frames from the Network tab around the failure
   window.
3. Open an issue with the captures attached.

The daemon emits enough structured fields (instance_id + seq + ts
on every envelope) that any reproduce-fail-investigate cycle can
be done from the captures alone without re-running the scenario.
