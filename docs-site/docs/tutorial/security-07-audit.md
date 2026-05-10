---
id: security-07-audit
title: "Security 7 - Audit log and observability"
sidebar_label: "Security 7: Audit"
---

The first six security tutorials all relied on the same daemon
behaviour: when a gate or a behaviour rule **rejects** a tool
call, the daemon writes a row that says *what was attempted, by
whom, when, and why it was refused*. That row is the audit
trail. Operators read it; incident response depends on it; the
daemon's own observability dashboards summarise it.

This tutorial walks through the audit data model, shows where to
read it, and demonstrates a real rejection landing in the log.

## What gets audited

The audit surface has three independent streams, each backed by
its own table.

| Stream                     | What it records                                    | Where to query                                                  |
|----------------------------|----------------------------------------------------|-----------------------------------------------------------------|
| **Session events**         | Every tool call (allowed and rejected), agent messages, hook fires, errors | `GET /api/apps/{app_id}/sessions/{session_id}/events`           |
| **Security gate audit**    | Per-gate decisions with the `gate1_*` / `gate4_*` codes | Admin endpoint (operator-only)                                   |
| **Credential audit (hash-chained)** | Read / create / refresh / revoke on every credential row | Admin endpoint, with `POST /api/admin/credentials/audit/verify`  |

The first stream is what an app developer reads to debug an agent
session. The other two are operator-side and are documented in
the [security reference](../language/11-security.md).

## What a rejected tool call looks like

Re-using the `custom-rule-bot` from
[Security 3](security-03-custom-rule.md): a behaviour rule
forbids writing inside `secrets/`. We send the agent a request
that the rule will block, then read the session's persistent
event log.

The agent attempts:

```text
> Write /tmp/secrets/secret.key with content "abc".
```

The behaviour engine catches it. Real audit row captured against
the daemon (one of 88 events for the session):

```json
{
  "type": "tool_call",
  "ts": "2026-05-09T07:49:29.297924+00:00",
  "payload": {
    "name": "Write",
    "success": false,
    "params": {
      "file_path": "/tmp/secrets/secret.key",
      "content": "abc"
    },
    "error": "[BEHAVIOR BLOCKED] Refused: writes inside secrets/ are blocked by the protect_secrets_dir rule.\nRule: protect_secrets_dir\nThe tool call was NOT executed. Fix the violation first."
  }
}
```

Five things are in there that matter for forensics:

- **Timestamp** in ISO-8601 with microsecond precision and UTC
  timezone.
- **Tool name** (`Write` → resolved to `filesystem.write` at
  dispatch time).
- **Full parameter set** as the LLM sent it. Useful for
  reconstructing what the agent intended.
- **`success: false`** flag.
- **Structured error** prefixed with `[BEHAVIOR BLOCKED]` and
  carrying the **rule id** that fired (`protect_secrets_dir`).
  Operators grep this prefix to count rule fires per app per
  day.

A successful call lands the same shape with `success: true` and
the tool's actual result. Approval-pending calls land with
`success: false` and `error: "[APPROVAL PENDING]"` until they
resolve.

## Reading the log from a script

The session event log is exposed at:

```text
GET /api/apps/{app_id}/sessions/{session_id}/events
    ?since_seq=0
    &limit=5000
```

Through the Python testing SDK:

```python
events = client.get_persistent_events(session)
for e in events:
    if e.get("type") == "tool_call" and not e["payload"].get("success", True):
        print(f"{e['ts']}  {e['payload']['name']}  -> {e['payload']['error']}")
```

Replace `get_persistent_events` with a `since_seq=N` parameter on
the raw GET to **resume** from where you stopped: live event
streams emit a monotonic `seq` per envelope; persistent events
preserve the same numbering. A reconnect that asks for events
since the last seq it saw gets exactly the missing window, with
no duplicates.

## What to count

For an operator dashboard, the useful metrics from the event log
are:

- **Failed tool calls per app per day** - any spike in
  `success: false` rate is a red flag. Either the LLM is
  trying things it shouldn't (capability misconfiguration) or
  the legitimate path is broken.
- **Approval-pending duration** - the gap between
  `tool_call (success: false, error: [APPROVAL PENDING])` and
  the matching `approval_resolved` event. Long gaps = a slow
  human reviewer.
- **Behavior rule fires per rule id** - lets you tune rule
  thresholds. A rule that fires on every other turn is too
  loose; a rule that never fires is dead code.
- **Per-session token cost** - extracted from `result` events
  with `usage.cost_usd`. Surface per-app totals for billing or
  budgeting.

The session event log retains everything for the configured
window (default 30 days, set in `daemon.session_events_retention_days`).
After that the rows roll off; archive externally if you need
longer retention.

## The credential audit chain

A separate concern, mentioned for completeness. The credentials
vault writes its own audit log to the `credential_audit` table.
Each row carries the SHA-256 hash of the **previous row plus the
current row's payload**, forming a tamper-evident chain.

A periodic verification job hits
`POST /api/admin/credentials/audit/verify`. The endpoint walks
the chain, recomputes each hash, and reports any divergence. Any
row that was deleted or modified after writing breaks the chain.
The verification result is itself logged, so even an attacker
who edits the database has to also edit the verification log
without leaving a trail.

The chain doesn't replace database backups - it complements them.
Backups protect against accidental loss; the chain detects
deliberate tampering.

## Pulling it together

The audit story has three layers, each with its own rate of
change:

1. **Session events**: high-volume, app-developer concern, daily
   query, archived weekly.
2. **Security gate audit**: lower-volume, operator concern,
   queried during incidents, archived monthly.
3. **Credential audit chain**: rare write, operator concern,
   verified continuously, archived **forever**.

The first one you'll touch every day; the other two are
infrastructure plumbing. The endpoints, retention defaults, and
verification cadence are documented in the
[security reference](../language/11-security.md) and the
[credentials reference](../reference/runtime/credentials.md).

## Going further

- The full security architecture, including the seven-gate
  audit codes:
  [Security architecture](../language/11-security.md).
- Real-time observability: the daemon's metrics endpoints
  surface aggregate counts of allowed / denied / pending tool
  calls per app:
  [Observability](../language/24-observability.md).
- The credentials vault's hash-chained audit log:
  [Credentials reference](../reference/runtime/credentials.md).
