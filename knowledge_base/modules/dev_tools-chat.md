---
id: dev_tools-chat
title: "dev_tools.chat (DevToolsChat)"
type: module-action
module: dev_tools
action: chat
fqn: dev_tools.chat
short_name: DevToolsChat
keywords: [dev_tools, chat, devtoolschat, dev]
permissions: []
risk_level: low
irreversible: false
require_approval: false
---

# dev_tools.chat (DevToolsChat)

## Description
Chat with a deployed app - sessions, queue, approvals, workspace, live events.

## Parameters
| Name | Type | Required | Default | Description |
|------|------|:--------:|---------|-------------|
| `app_id` | string |  | `` | App ID (required for first message). |
| `message` | string |  | `` | Message to send. |
| `workspace` | string |  | `` | Workspace directory path. |
| `session_id` | string |  | `` | Session ID (follow-ups, inspect). |
| `client_message_id` | string |  | `` | Optional idempotency key for this send. |
| `queue_mode` | string |  | `` | 'async' \| 'wait' \| 'replace_last'. |
| `image_paths` | array |  | - | Paths to images to attach. |
| `inspect` | boolean |  | `False` | Inspect session - turns, tools, violations. |
| `memory` | boolean |  | `False` | Get session memory (goal, facts, entities). |
| `tasks` | boolean |  | `False` | Get session task list. |
| `get_workspace` | boolean |  | `False` | Get workspace snapshot (files + state). |
| `preview_snapshot` | boolean |  | `False` | Get preview snapshot (UI state). |
| `code_snapshot` | boolean |  | `False` | Get code snapshot (file tree without content). |
| `file_path` | string |  | `` | Read a specific workspace file. |
| `approve_file` | string |  | `` | Approve a workspace file by path. |
| `reject_file` | string |  | `` | Reject a workspace file by path. |
| `history` | boolean |  | `False` | Get session message history. |
| `persistent_events` | boolean |  | `False` | Get persistent event log (DB). |
| `since_seq` | integer |  | `0` | Replay events since this seq. |
| `context_breakdown` | boolean |  | `False` | Get per-source token breakdown. |
| `queue` | boolean |  | `False` | Get queue entries for the session. |
| `clear_queue` | boolean |  | `False` | Clear all queued messages. |
| `cancel_entry_id` | string |  | `` | Cancel a queue entry by id. |
| `abort` | boolean |  | `False` | Abort the current turn. |
| `purge_queue_on_abort` | boolean |  | `False` | Purge queue on abort. |
| `resume` | boolean |  | `False` | Resume an interrupted session. |
| `fork` | boolean |  | `False` | Fork the session. |
| `compact` | boolean |  | `False` | Compact context. |
| `export_session` | boolean |  | `False` | Export session as JSON. |
| `delete_session` | boolean |  | `False` | Delete the session permanently. |
| `respond` | string |  | `` | Respond to an ask_user question. |
| `approve_id` | string |  | `` | Approve a pending tool call by request_id. |
| `deny_id` | string |  | `` | Deny a pending request. |
| `pending` | boolean |  | `False` | List pending approvals/questions. |
| `search` | string |  | `` | Search sessions of app_id by query. |
| `list_sessions` | boolean |  | `False` | List all sessions of app_id. |
| `watch` | boolean |  | `False` | Live-stream the turn: receive events in real time, return early on approval/ask_user/error. |
| `watch_include_tokens` | boolean |  | `False` | Include per-token events in the timeline (verbose). |
| `watch_max_events` | integer |  | `200` | Max events returned in the timeline. |
| `timeout` | number |  | `3600.0` | Max wait time. |

## Capability grant (in app YAML)
```yaml
capabilities:
  grant:
    - module: dev_tools
      actions: [chat]
```

## Tool usage instructions
```
Exercise conversational apps like a human user would, plus everything the Flutter client shows: live events, queue state, preview snapshot, code snapshot, workspace files, memory, tasks, history, approvals, ask_user, abort/resume/fork.

## Send messages
  Chat(app_id='my-app', message='...', workspace='/path')  - new session, return session_id
  Chat(session_id='s', message='...')                     - follow-up
  Chat(session_id='s', message='...', queue_mode='async') - send while turn running (queue)
  Chat(session_id='s', image_paths=['a.png','b.png'], message='describe')  - multimodal

## Watch mode (PREFERRED for testing - avoid timeouts)
  Chat(app_id='x', message='...', watch=true)
  Returns a compact seq-ordered timeline (tool_calls, text chunks, thinking,
  approvals, errors) and an explicit status: 'completed' | 'pending_approval' |
  'pending_ask_user' | 'error' | 'timeout'. Returns EARLY on blockers - no waste.
  If pending_ask_user: follow up with respond='<answer>'.
  If pending_approval: follow up with approve_id=<rid>.

## Inspect
  Chat(session_id='s', inspect=true)          - turns + tools + violations
  Chat(session_id='s', memory=true)           - goal, todos, facts
  Chat(session_id='s', tasks=true)            - task list
  Chat(session_id='s', history=true)          - full message history
  Chat(session_id='s', persistent_events=true, since_seq=N)  - durable event log
  Chat(session_id='s', context_breakdown=true)  - token breakdown

## Workspace / preview
  Chat(session_id='s', get_workspace=true)    - workspace metadata
  Chat(session_id='s', preview_snapshot=true) - UI state
  Chat(session_id='s', code_snapshot=true)    - file tree (no content)
  Chat(session_id='s', file_path='src/x.py')  - specific file content
  Chat(session_id='s', approve_file='src/x.py') / reject_file=...

## Queue / control
  Chat(session_id='s', queue=true)            - list queue
  Chat(session_id='s', clear_queue=true) / cancel_entry_id=...
  Chat(session_id='s', abort=true, purge_queue_on_abort=true)
  Chat(session_id='s', resume=true)           - after crash/interrupt
  Chat(session_id='s', fork=true) / compact=true / export_session=true / delete_session=true

## Approvals / ask_user
  Chat(session_id='s', pending=true)          - what's blocking
  Chat(session_id='s', respond='my answer')   - answer ask_user
  Chat(session_id='s', approve_id='<rid>') / deny_id='<rid>'

## Find sessions
  Chat(app_id='my-app', list_sessions=true)
  Chat(app_id='my-app', search='<query>')

## Rules
- Use realistic messages (not 'test')
- At least 2-3 turns to validate multi-turn memory
- If the agent blocks: pending=true first, then respond= or approve_id=
- Always inspect after a test - tools_used, used_bash_for_files, violations
```

## Safety
- Risk level: **low**
