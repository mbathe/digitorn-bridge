# Agent Spawn Module

Spawn isolated sub-agents that run in true parallelism. Each sub-agent has its own context window, messages, and tools. The coordinator is notified when agents complete or fail.

## Tool surface

One `Agent` tool with 8 modes (selected by params):

| Mode | Call shape | Use |
| ---- | ---------- | --- |
| 1 | `Agent(prompt='...')` | Spawn (background, default), returns `agent_id` |
| 2 | `Agent(prompt='...', wait=true)` | Spawn + block until done |
| 3 | `Agent(agent_id='...')` | Status check |
| 4 | `Agent(agent_id='...', wait=true)` | Wait for one |
| 5 | `Agent(agent_ids=[...])` | Wait for many (gather) |
| 6 | `Agent(agent_id='...', cancel=true)` | Cancel |
| 7 | `Agent(agent_id='...', reassign='new task')` | Respawn |
| 8 | `Agent(list=true)` | List all |

## Inheritance contract

Each sub-agent inherits from the coordinator:

- **Workspace** + **security_profile** + **compiled_constraints** + **sandbox_worker** + **direct_modules_map** + **approval_queue** + **user_id** + **app_id** + **current_run_id** (parent run id, propagates to `agent_runs.parent_run_id`).
- **Memory seed** — read-only snapshot of `original_request`, `goal`, `sub_goals`, `todos`, `key_facts`. Capped (see config below) and rendered as the `## Inherited context` block above the specialist prompt. Sub-agents **must not** call `SetGoal` / `TodoAdd` / `TodoUpdate` — that is the coordinator's responsibility.
- **Cooperative cancel** — `tracked.cancel_event` checked at the top of each `agent_turn`. The cancel mode flips the event before issuing `task.cancel()` so the loop bails at the next turn boundary even if the asyncio cancel signal gets swallowed.
- **Shared module instances** for `memory`, `web`, `lsp`, `filesystem`, `shell` (same workspace + same memory store as the coordinator). All other modules get fresh per-spawn instances cached per session (see LRU below).
- **Provider clone** per agent (own httpx pool, no shared connection state).

## Real-time event pipeline

```text
Sub-agent finit
  │
  ├─► tracked.set_result_and_signal(result)            (B1: signal Event waiters)
  │
  ├─► notify_fn(agent_<status> payload)
  │     │
  │     └─► context_builder.push_module_notification
  │           │
  │           ├─► queue.put_nowait                     (drained next turn — slow path)
  │           ├─► _on_notification_relay              → agent_event Socket.IO  (frontend live)
  │           └─► _on_terminal_agent_event            → manager.check_notifications()  (≤ms)
  │                                                       │
  │                                                       └─► drain queue
  │                                                       └─► format_agent_notification (structured)
  │                                                       └─► agent_turn (coordinator wakes)
  │
  └─► task transitions to done
        └─► watchdog _on_done                          (synthesize terminal state if missing)
```

The coordinator sees structured `[SUB-AGENT COMPLETED]` / `[SUB-AGENT FAILED]` / `[SUB-AGENT CANCELLED]` / `[SUB-AGENT TIMEOUT]` blocks in its system message stream — not the generic `[BACKGROUND TASK …]` envelope. Format defined in `core/runtime/notifications.py::format_agent_notification`.

## Configuration

All settings live under `agent_spawn:` in `~/.digitorn/config.yaml` (or `DIGITORN_AGENT_SPAWN__*` env vars). Defaults below.

| Field | Default | Range | Purpose |
| ----- | ------- | ----- | ------- |
| `max_workers` | 20 | 1-500 | Max parallel sub-agents per session. |
| `max_workers_global` | 200 | 1-2000 | Hard ceiling on total concurrent sub-agents across all sessions. |
| `max_turns` | 100 | 10-10000 | Default per-sub-agent turn cap. |
| `timeout` | 3600 | 30-7200 | Default sub-agent timeout (s). |
| `cleanup_age` | 300 | 30-86400 | Drop completed sub-agents from registry after N s. |
| `cleanup_interval` | 30 | 5-600 | Periodic cleanup task cadence (also triggers LRU cache eviction). |
| `max_seed_total_chars` | 4000 | 500-20000 | Hard cap on the inherited-context block. Per-section caps (5 sub-goals, 8 todos, 7 facts, 300 char/item, 500 char/header) apply first. |
| `max_cached_sessions` | 100 | 10-10000 | LRU bound on the per-session module/ContextBuilder cache. Each entry holds a full module set + index — keep tight. |

## Observability

### Prometheus metrics (`/api/metrics`)

| Type | Name | Labels |
| ---- | ---- | ------ |
| Counter | `digitorn_agent_spawn_total` | `app_id`, `specialist` |
| Counter | `digitorn_agent_completed_total` | `app_id`, `specialist` |
| Counter | `digitorn_agent_failed_total` | `app_id`, `specialist` |
| Counter | `digitorn_agent_cancelled_total` | `app_id`, `specialist` |
| Counter | `digitorn_agent_timeout_total` | `app_id`, `specialist` |
| Counter | `digitorn_agent_unknown_total` | `app_id`, `specialist` |
| Gauge | `digitorn_agent_running` | `app_id`, `session_id` |
| Histogram | `digitorn_agent_duration_seconds` | `specialist`, `status` |

The `unknown` counter fires when the watchdog races ahead of result finalisation — should be 0 in steady state. A non-zero rate signals a hot bug in the runner's terminal path.

Wired in `module.py::_emit_spawn_metric` (paired with `_bump_running` in `_mode_spawn`) and `module.py::_emit_terminal_metric` (paired with `_drop_running` in `_install_agent_watchdog._on_done`). Both helpers swallow backend errors so a faulty metrics layer never breaks the spawn / watchdog hot paths.

### Distributed tracing (`current_trace()`)

Every sub-agent run opens a `sub_agent_run` span on the parent's `TraceContext` (propagated via contextvar — `asyncio.create_task` inherits it). The span appears as a child of the coordinator's `agent_turn`, so the full fan-out tree is visible in one trace dump.

Span attributes:

| Attribute | Source |
| --------- | ------ |
| `agent_id` | spawn-time |
| `specialist` | spawn-time |
| `parent_run_id` | spawn-time |
| `app_id` | spawn-time |
| `status` | terminal (`completed` / `failed` / `cancelled` / `timeout` / `unknown`) |
| `turns_used` | terminal |
| `tool_calls_count` | terminal |
| `duration_seconds` | terminal |
| `errors_count` | terminal (only when > 0) |

`span.status = "error"` on `failed` / `timeout`. `cancelled` keeps `status = "ok"` (cancellation is not an error).

## Hardening summary (Phase A + B + C)

- **Per-session spawn locks** — single global lock was the bottleneck under fan-out. Now per-`session_id` so different sessions spawn in parallel.
- **O(1) running counters** — `_bump_running` / `_drop_running` maintain `_running_count_by_session` + `_total_running_count` instead of iterating the agent registry on every capacity check.
- **Periodic async cleanup** — replaces the inline `_cleanup_completed()` that ran on every spawn. Configurable cadence via `cleanup_interval`. Also triggers LRU cache eviction.
- **Structural result signaling** — `TrackedAgent.result_event` (asyncio.Event) replaces 5x `await asyncio.sleep(0.05)` race guards in `_mode_wait_one`.
- **Memory inheritance** — `_capture_parent_memory_seed` snapshots the parent's working memory under the spawn lock; `_format_parent_memory_seed` caps + truncates each section.
- **Cooperative cancel** — `cancel_event` flipped before `task.cancel()` so the agent loop bails at a turn boundary even when asyncio cancel gets swallowed.
- **Watchdog terminal synthesis** — `add_done_callback` synthesizes `tracked.result` whenever the runner crashes before producing one. Single point that always runs `_drop_running` + emit terminal metric.
- **Direct terminal-event push** — `_on_terminal_agent_event` pings `manager.check_notifications` synchronously so the coordinator wakes in ms, not in the next 1 s polling tick.

## File map

| File | Responsibility |
| ---- | -------------- |
| `module.py` | Tool dispatch (8 modes), spawn locks, counters, watchdog, cleanup task, metrics emission, parent_memory_seed capture |
| `runner.py` | `run_isolated_agent` wrapper (opens `sub_agent_run` span) → `_run_isolated_agent_impl` (full lifecycle), per-session module cache, `TrackedAgent` dataclass, `AgentResult` |
| `params.py` | `AgentParams` (8-mode union), hidden params filter |
| `bootstrap.py` | Specialist registration, action_filter compilation, `_inject_app_id_overrides` for shared modules |

## See also

- `core/runtime/notifications.py` — `format_agent_notification` / `format_bg_task_notification`
- `core/metrics.py` — `MetricsCollector` (Prometheus exposition)
- `core/tracing.py` — `Tracer`, `TraceContext`, `SpanRecord`
- `core/app/manager_v2/_deploy.py` — `_terminal_agent_bridge` wiring
