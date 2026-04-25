---

id: orchestration
title: Orchestration Engine
sidebar_position: 4
format: md
---



# Orchestration Engine

The orchestration layer drives the full lifecycle of every plan from submission to completion. It encompasses DAG-based scheduling, state persistence, approval gates, rollback compensation, resource management, action streaming, result truncation, and distributed node routing.

---


## DAG Scheduler

Plans with `execution_mode = "parallel"` are scheduled using a networkx-based DAG.

### ExecutionWave

Actions are grouped into waves of independent operations:

| Field | Type | Description |
|-------|------|-------------|
| `wave_index` | int | Wave sequence number |
| `action_ids` | list[str] | Actions in this wave |
| `is_final` | bool | Whether this is the last wave |

### DAGScheduler

| Method | Description |
|--------|-------------|
| `waves()` | Iterator of ExecutionWave (parallel or sequential) |
| `topological_order()` | Full topological sort |
| `successors(action_id)` | Direct downstream actions |
| `predecessors(action_id)` | Direct upstream actions |
| `ancestors(action_id)` | All transitive upstream actions |
| `descendants(action_id)` | All transitive downstream actions |
| `is_independent(a, b)` | Whether two actions can run concurrently |

**Wave generation**: In parallel mode, each wave contains all actions whose dependencies have been satisfied. Actions within a wave execute concurrently.

```
Wave 0: [A, B]        ← no dependencies
Wave 1: [C, D]        ← depend on A or B
Wave 2: [E]           ← depends on C and D
```

---


## State Machine

### ActionState

| Field | Type | Description |
|-------|------|-------------|
| `action_id` | string | Action identifier |
| `status` | ActionStatus | Current status |
| `started_at` | float | Execution start timestamp |
| `finished_at` | float | Execution end timestamp |
| `result` | Any | Action result |
| `error` | string | Error message if failed |
| `attempt` | int | Current retry attempt |
| `module` | string | Module ID |
| `action` | string | Action name |
| `alternatives` | list | Suggested alternative actions |
| `fallback_module` | string | Module used via fallback chain |
| `approval_metadata` | dict | Approval decision details |

### ExecutionState

| Field | Type | Description |
|-------|------|-------------|
| `plan_id` | string | Plan identifier |
| `plan_status` | PlanStatus | PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, PAUSED |
| `created_at` | float | Creation timestamp |
| `updated_at` | float | Last update timestamp |
| `actions` | dict | ActionState per action ID |
| `rejection_details` | dict | Scanner/verifier rejection info |

Methods: `from_plan(plan)`, `get_action(id)`, `all_completed()`, `any_failed()`, `to_dict()`.

### PlanStateStore

SQLite-backed persistence with WAL mode:

| Method | Description |
|--------|-------------|
| `init()` | Create tables and initialize connection |
| `close()` | Close database connection |
| `create(state)` | Insert new execution state |
| `update_plan_status(plan_id, status, rejection_details)` | Update plan status |
| `update_action(plan_id, action_id, status, result, error, attempt)` | Update action status |
| `get(plan_id)` | Retrieve execution state |
| `list_plans(status, limit)` | List plans with optional filter |
| `purge_old_plans(retention_seconds)` | Clean up old plans |

---


## Approval System

### ApprovalDecision

| Decision | Behavior |
|----------|----------|
| `APPROVE` | Execute the action |
| `REJECT` | Skip the action, mark as failed |
| `SKIP` | Skip the action, mark as skipped |
| `MODIFY` | Execute with modified parameters |
| `APPROVE_ALWAYS` | Approve and auto-approve future identical requests |

### ApprovalRequest

| Field | Type | Description |
|-------|------|-------------|
| `plan_id` | string | Plan identifier |
| `action_id` | string | Action identifier |
| `module` | string | Module ID |
| `action_name` | string | Action name |
| `params` | dict | Action parameters |
| `risk_level` | string | low/medium/high/critical |
| `description` | string | Human-readable description |
| `requires_approval_reason` | string | Why approval is required |
| `clarification_options` | list | Structured options |
| `requested_at` | float | Request timestamp |

### ApprovalGate

Async coordination between executor and API:

| Method | Description |
|--------|-------------|
| `request_approval(request, timeout, timeout_behavior)` | Block until decision (executor side) |
| `submit_decision(plan_id, action_id, response)` | Submit decision (API side) |
| `get_pending(plan_id)` | List pending approvals |
| `is_auto_approved(module, action)` | Check auto-approval status |
| `clear_auto_approvals()` | Reset all auto-approvals |

The executor calls `request_approval()` which blocks on an `asyncio.Event`. The API endpoint calls `submit_decision()` which sets the event. Timeout behavior is configurable: `reject` or `skip`.

---


## Rollback Engine

Compensating actions when `on_error = "rollback"`:

```
Action fails
    |
    v
RollbackEngine.execute(plan, failed_action, results)
    |
    +--→ Look up rollback.action reference
    |
    +--→ Resolve {{result.X.Y}} templates in rollback params
    |
    +--→ Dispatch compensating action to module
    |
    +--→ Max depth: 5 (prevents infinite rollback chains)
```

---


## Plan Groups

Execute multiple independent plans concurrently:

### PlanGroupResult

| Field | Type | Description |
|-------|------|-------------|
| `group_id` | string | Group identifier |
| `status` | string | `completed`, `partial_failure`, `failed` |
| `plan_results` | dict | ExecutionState per plan |
| `errors` | dict | Error messages per failed plan |
| `started_at` | float | Group start time |
| `finished_at` | float | Group end time |

Properties: `duration`, `summary` (count by status).

### PlanGroupExecutor

```python
executor = PlanGroupExecutor(plan_executor)
result = await executor.execute(
    plans=[plan1, plan2, plan3],
    max_concurrent=10,
    timeout=300.0,
)
```

API endpoint: `POST /plan-groups`

---


## Resource Management

Per-module concurrency control via semaphores:

```python
manager = ResourceManager(
    limits={"excel": 3, "word": 3, "browser": 2},
    default_limit=10,
)

async with manager.acquire("excel"):
    # Only 3 concurrent excel operations allowed
    await module.execute(action, params)
```

| Method | Description |
|--------|-------------|
| `acquire(module_id)` | Async context manager, waits for semaphore |
| `status()` | Current usage per module |

Configuration:
```yaml
resource:
  default_concurrency: 10
  module_limits:
    excel: 3
    word: 3
    browser: 2
```

---


## Action Streaming

### ActionStream

Injected into `params["_stream"]` for `@streams_progress` actions:

| Method | Description |
|--------|-------------|
| `emit_progress(percent, message)` | Progress update (0-100%, clamped) |
| `emit_intermediate(data)` | Partial results |
| `emit_status(status)` | Status change ("connecting", "transferring", etc.) |

All methods emit to `TOPIC_ACTION_PROGRESS` via EventBus.

### SSE Endpoint

`GET /plans/{plan_id}/stream` provides Server-Sent Events:

```
event: action_progress
data: {"plan_id":"p1","action_id":"a1","percent":50.0,"message":"halfway"}

event: action_result_ready
data: {"plan_id":"p1","action_id":"a1","status":"completed","result":{...}}

event: plan_completed
data: {"plan_id":"p1"}

: keepalive
```

Features:
- Plan-specific filtering (only events for the requested plan_id)
- 30-second keepalive heartbeats
- Automatic listener cleanup on disconnect
- `_serialisable()` helper strips non-JSON-safe underscore keys (preserves `_topic`, `_timestamp`)

### Result Events

The executor emits `action_result_ready` events on TOPIC_ACTION_RESULTS for ALL completed actions (not just `@streams_progress` ones). This enables the SDK to receive immediate completion notifications without polling.

---


## Result Truncation

Module results are truncated to prevent LLM context overflow:

| Constant | Value | Description |
|----------|-------|-------------|
| `_DEFAULT_MAX_RESULT_SIZE` | 524,288 (512 KB) | Maximum result size in bytes |
| `_BINARY_RESULT_KEYS` | frozenset | Keys excluded from size check |

Binary keys excluded: `screenshot_b64`, `labeled_image_b64`, `image_b64`, `content_b64`, `thumbnail_b64`, `preview_b64`.

Oversized results are replaced with a summary:
```json
{
  "_truncated": true,
  "_original_size": 1048576,
  "_max_size": 524288,
  "_message": "Result too large, truncated",
  "_keys": ["data", "rows", "metadata"]
}
```

---


## Distributed Nodes

### BaseNode (ABC)

| Method | Description |
|--------|-------------|
| `node_id` | Unique node identifier (property) |
| `execute_action(module_id, action, params)` | Dispatch action to node |
| `is_available()` | Node health check |

### LocalNode

Default implementation that dispatches to the local `ModuleRegistry`.

### NodeRegistry

| Method | Description |
|--------|-------------|
| `resolve(target)` | Find node for action dispatch |
| `register(node)` | Add remote node |
| `unregister(node_id)` | Remove node |
| `list_nodes()` | All registered node IDs |

When `IMLAction.target_node` is set, the executor uses `NodeRegistry.resolve()` to route the action to the appropriate node.

---


## Fallback Chains

Module fallback for graceful degradation:

```yaml
module:
  fallbacks:
    excel: ["filesystem"]
    browser: ["api_http"]
```

When a module action fails, the executor tries fallback modules in order. The `fallback_module` field in `ActionState` records which module actually executed.

### Alternative Suggestions

When an action fails, `_suggest_alternatives()` proposes alternative actions based on the error and available modules.
