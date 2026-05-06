# Digitorn Postgres v2 - design & migration reference

> **Status (2026-05-05)** - validated by user, 8 sprint migrations
> written, ready to apply on Neon. Each sprint is one Alembic
> revision, idempotent, safe to re-run.

This document is the single source of truth for the v2 database
schema and the migration path that produced it. Read it before
making any change to `packages/digitorn/core/models.py` or to any
migration in `packages/digitorn/core/migrations/versions/`.

## Why we did this

The pre-v2 database carried the full lineage of an exploratory
project: tables that had once been useful but no longer received
writes (`quota_definitions`, `usage_events`), columns missing audit
trails (`updated_at` absent on 8 tables), JSON columns that couldn't
be indexed, and - most importantly - **no first-class agent run
tracking**. The existing `agents` table held one row per
(session, agent_id) with no sense of "this run started, used N
tokens, completed at T". The dashboard couldn't answer "who's
running right now?" because the data simply wasn't recorded.

V2 fixes the gap with a 4-level tracking hierarchy and a parallel
gateway billing schema, while bringing the whole DB up to standards
typical of a Microsoft / production-grade system: indexed JSONB,
foreign keys with explicit ON DELETE behaviour, partitioning for
time-series tables, soft delete where users care about undo, RLS
for tenant isolation, and audit trails on every user-facing row.

## Two deployment modes (cloud vs local)

The runtime supports two operational modes, chosen at boot via
``settings.runtime.tracking.backend``. Five backends are bundled, and
custom ones plug in without modifying the runtime.

| Key          | Mode  | Storage                                                          | Best for                              |
|--------------|-------|------------------------------------------------------------------|---------------------------------------|
| ``postgres`` | cloud | Neon / any Postgres + v2 schema                                  | Production on ``api.digitorn.ai``     |
| ``sqlite``   | local | One file PER SESSION (`<root>/<app_id>/<sid>/runs.sqlite`)       | Single laptop, queryable history      |
| ``kv``       | local | One file PER SESSION (`<root>/<app_id>/<sid>/runs.kv`, stdlib)   | Zero-deps minimal, O(1) lookup        |
| ``jsonfile`` | local | One JSONL PER SESSION (`<root>/<app_id>/<sid>/runs.jsonl`)       | Tail-friendly, ad-hoc ``jq`` analysis |
| ``null``     | off   | Discards all events                                              | Benchmarks, sandbox                   |

The Postgres backend is the only one that hooks into the dashboard
views (``v_agents_running``, ``v_agents_top_cost_7d``, …). Local
backends store the same data shape but readers handle their own
aggregation.

Default path can be overridden per-backend through
``runtime.tracking.config: {path: "/var/lib/digitorn/runs"}``. The
local backends create one subdirectory per session under that root.

### Per-session layout (local backends)

Every local backend (``sqlite``, ``kv``, ``jsonfile``) keeps each
session's data in its own file under
``<root>/<app_id>/<external_sid>/runs.<ext>``. Two parallel sessions
write to two distinct files, so their writes do not contend on a
shared lock - the OS filesystem schedules them independently. A
session's data is bounded by that session's activity (not by the
daemon's lifetime), and ``rm -rf <session_dir>`` is the only command
needed to forget a session.

A small global index file at the root,
``<root>/_run_index.sqlite``, maps ``run_id`` to its session
directory. The index is loaded into memory at backend ``setup()`` so
hot-path lookups never touch disk; writes happen only on
``start_run`` (one tiny INSERT). The index file survives daemon
restarts: a run that started before a crash is still routable to its
file when the daemon comes back up. Implementation:
``digitorn.core.runtime.run_tracker.backends._perfile.PerSessionRouter``.

Custom backends: implement
``digitorn.core.runtime.run_tracker.protocols.TrackerBackend`` and
register it in ``BACKEND_REGISTRY`` before the daemon's lifespan
fires - no other code change required.

### Producer vs drain latency

The "loop is fast" guarantee is about the **producer** side - the
synchronous enqueue the runtime calls. Measured at boot of a
current-generation laptop with each bundled backend:

| Backend      | Producer (loop)  | Drain (worker, 200 runs × 4 events)            |
|--------------|------------------|------------------------------------------------|
| ``null``     | ~20 us / turn    | ~1 ms                                          |
| ``sqlite``   | ~19 us / turn    | several seconds (one connect/close per call)   |
| ``kv``       | ~18 us / turn    | several seconds (open/sync/close per write)    |
| ``jsonfile`` | ~24 us / turn    | ~2 s                                           |

The loop pays only the producer cost - **20 µs per turn, every
backend, identical for the runtime**. Drain happens off the hot path;
when it can't keep up, events queue (capped 10 000) then drop with
a WARNING. Drain throughput can be improved later by keeping a single
connection open across the worker's lifetime (currently the ``sqlite``
and ``kv`` backends open per call for exception safety).

## Performance contract: the agent loop never blocks on persistence

This is a hard architectural rule. ``agent_turn`` and the inner
``_loop`` MUST NOT await DB I/O for tracking purposes. The previous
inline tracker was awaiting 3-5 round trips to Postgres per turn (40
to 200 ms), strangling the latency it was supposed to measure.

The current implementation:

  1. Every public ``run_tracker.start_run`` /
     ``emit_event`` / ``complete_run`` / ``increment_*`` call is
     **synchronous and non-blocking**. It captures args into a dict
     and ``put_nowait`` into an asyncio queue. Returns in microseconds.
  2. ``run_id`` is generated **client-side** via UUID4 hex - no
     SELECT/INSERT round-trip is needed to obtain it before the
     loop continues.
  3. A single background worker (``run_tracker.worker._drain_loop``)
     drains the queue, awaiting the configured backend's async API.
     If the backend is slow, the queue grows; if it caps, events are
     dropped with a WARNING. The producer is never blocked.
  4. Per-run sequence allocation happens in the producer (in-memory
     counter), so backends never need a ``SELECT MAX(sequence)``.
  5. The worker is started by the daemon's lifespan AFTER ``init_db``
     and stopped BEFORE ``close_db`` with a 5-second drain.

Measured overhead on a current-generation laptop:

| Operation                           | Avg latency  |
|-------------------------------------|--------------|
| ``start_run`` (1 enqueue)           | ~9 us        |
| ``emit_event`` (1 enqueue)          | ~30 us       |
| ``complete_run`` (1 enqueue)        | ~31 us       |
| Full turn lifecycle (6 enqueues)    | ~35 us       |

Compare to the previous blocking implementation: ~50 ms / turn on
Neon. Speedup ~1450x.

## Cost is gateway-only

The runtime never writes ``agent_runs.cost_breakdown``. Cost is
computed and persisted by the gateway in ``gateway_usage_events``,
where one row per LLM call carries
``cost_breakdown JSONB`` (per-provider sub-totals) and a
trigger-materialised ``total_cost_usd``. The dashboard view
``v_agents_top_cost_7d`` aggregates cost by joining
``agent_runs`` to ``gateway_usage_events`` on ``user_id``.

Why: the gateway is the only component that knows the rate card and
sees every dispatch in real time. Forwarding cost back to the daemon
to write into ``agent_runs.cost_breakdown`` would be a wasteful
round-trip and would slow the runtime - which is the one thing this
architecture refuses.

When the gateway must block a user (quota exceeded), it does so at
the streaming HTTP boundary by emitting a
``quota_exceeded`` SSE error - the runtime sees it as any other LLM
error and surfaces it to the user. The runtime contains zero quota
logic.

## The 4-level agent tracking hierarchy

```
                       users
                         │
                  user_sessions
                         │
                  session_agents          (one per (session, specialist))
                  ┌──────┴──────┐
            agent_runs   …N more (one per spawn / wait-for cycle)
            ┌─────┴────────────────┐
agent_run_events                action_executions
(append-only timeline)        (per-tool result detail)
```

Each level answers a different question:

| Level                | Question it answers                                    |
|----------------------|--------------------------------------------------------|
| `user_sessions`      | Which conversation is this?                            |
| `session_agents`     | Which specialist participated?                         |
| `agent_runs`         | When did this specific run happen, what did it use?    |
| `agent_run_events`   | What's the live timeline of that run?                  |
| `action_executions`  | What params did each tool get, what did it return?     |

`agent_runs` is the heart. One row per launch (foreground OR
background). Lifecycle: `queued → active → completed | failed |
cancelled | timeout | paused`. The `total_tokens` and `duration_ms`
columns are GENERATED (Postgres 12+), so the dashboard never has to
recompute them on read. `total_cost_usd` is materialised by trigger
from `cost_breakdown JSONB` (one column per provider, summed
on every UPDATE of `cost_breakdown`).

## The 8 migration sprints

| Sprint | Revision | Scope                                                                                              | Risk    |
|--------|----------|----------------------------------------------------------------------------------------------------|---------|
| A      | 0001     | Drop dead tables, add 4 missing FK indexes, add `updated_at` to 8 tables, widen `history_log.id` to BIGINT | Zero    |
| B      | 0002     | All JSON → JSONB, GIN indexes on hot search paths                                                  | Low     |
| C      | 0003     | Rename `agents` → `session_agents`; create `agent_runs`, `agent_run_events`; refactor FKs; add views | Medium  |
| D      | 0004     | Add `status`/`title`/`deleted_at` to `user_sessions`; rename `session_id` → `external_sid`         | Low     |
| E      | 0002 (gateway) | Extend gateway tables; add `gateway_user_plan_history`; create partitioned `gateway_usage_events` | Medium  |
| F      | 0005     | Fuse `inbox_devices`+`inbox_notification_prefs` → `user_devices`; add `feature_flags`, `audit_actions_catalog`; soft delete columns | Low     |
| G      | 0006     | Partition `history_log` by month                                                                   | High    |
| H      | 0007     | Row-Level Security on user-owned tables                                                            | Low     |

Daemon migrations live in `packages/digitorn/core/migrations/`,
gateway migrations in `packages/gateway/alembic/`. They share the
same Postgres but use **separate Alembic version tables** so they
upgrade independently:

  * `alembic_version_daemon`
  * `alembic_version_gateway`

Order of application:

```
daemon  0001 → 0002 → 0003 → 0004 → 0005 → 0006 → 0007
gateway 0001 (already applied) → 0002
```

Sprint E (gateway 0002) creates `gateway_usage_events.run_id` as a
**soft cross-service reference** to `agent_runs.id`. We deliberately
avoided a DB-level foreign key so the gateway service can run
independently of the daemon's deploy cadence. The application layer
maintains referential integrity.

## Final state (37 tables, 10 domains)

### 1. Auth (4)
`users`, `user_roles`, `roles`, `role_permissions`

### 2. Sessions (3)
`user_sessions`, `session_agents`, `session_checkpoints`

### 3. Agent runtime (4)
`agent_runs`, `agent_run_events`, `action_executions`, `activations`

### 4. Apps (4)
`applications`, `app_bundles`, `app_module_configs`, `app_profiles`

### 5. Credentials (3)
`credentials`, `credential_grants`, `credential_audit`

### 6. Gateway (6)
`gateway_plans`, `gateway_user_plans`, `gateway_user_plan_history`,
`gateway_quota_counters`, `gateway_quota_blocks`,
`gateway_usage_events`

### 7. History / audit (2)
`history_log` (partitioned monthly), `audit_actions_catalog`

### 8. Notifications (1)
`user_devices`

### 9. Features (1)
`feature_flags`

### 10. System (5)
`alembic_version_daemon`, `alembic_version_gateway`,
`api_keys`, `refresh_tokens`, `hub_sessions`

(The numbers above don't include legacy tables that are scheduled
for drop in a follow-up sprint: `inbox_devices`,
`inbox_notification_prefs`, `inbox_items` once `user_devices` is
the sole writer; `agents` is renamed not duplicated.)

## Detailed table reference

### `agent_runs` (the heart of agent tracking)

| Column                 | Type                  | Notes                                                  |
|------------------------|-----------------------|--------------------------------------------------------|
| `id`                   | VARCHAR(64) PK        | hex uuid                                               |
| `session_agent_id`     | VARCHAR(64) FK CASCADE| references `session_agents.id`                         |
| `session_pk`           | VARCHAR(64) FK CASCADE| denormalised for cheap joins                           |
| `user_id`              | VARCHAR(64) FK CASCADE| denormalised for RLS / per-user quota                  |
| `parent_run_id`        | VARCHAR(64) FK SET NULL| set when spawned by another agent                     |
| `status`               | VARCHAR(16)           | `queued | active | completed | failed | cancelled | timeout | paused` |
| `status_reason`        | TEXT                  | populated when `failed | cancelled | timeout`          |
| `specialist`           | VARCHAR(64)           | which agent persona                                    |
| `provider` / `model`   | VARCHAR               | LLM provider + model name                              |
| `fallback_used`        | BOOLEAN               | true when AgentBrain fallback fired                    |
| `task_summary`         | TEXT                  | short description of the run's task                    |
| `max_turns`            | INTEGER               | hard cap; null = inherit app default                   |
| `turns_used`           | INTEGER               | counter                                                |
| `sub_agents_spawned`   | INTEGER               | counter                                                |
| `prompt_tokens`        | BIGINT                | counter                                                |
| `completion_tokens`    | BIGINT                | counter                                                |
| `cache_read_tokens`    | BIGINT                | counter (Anthropic cache)                              |
| `cache_write_tokens`   | BIGINT                | counter (Anthropic cache)                              |
| `total_tokens`         | BIGINT GENERATED      | sum of the four above                                  |
| `cost_breakdown`       | JSONB                 | `{provider: {input_usd, output_usd, cache_usd, total_usd}}` |
| `total_cost_usd`       | NUMERIC(14,6)         | trigger-materialised from `cost_breakdown`             |
| `queued_at`            | TIMESTAMPTZ           | server clock                                           |
| `started_at`           | TIMESTAMPTZ           | when status went `queued → active`                     |
| `completed_at`         | TIMESTAMPTZ           | when status went terminal                              |
| `last_event_at`        | TIMESTAMPTZ           | heartbeat from `agent_run_events`                      |
| `duration_ms`          | BIGINT GENERATED      | `completed_at - started_at` in ms; null while active   |
| `deleted_at`           | TIMESTAMPTZ           | soft delete                                            |
| `created_at`/`updated_at` | TIMESTAMPTZ        | audit; trigger-managed                                 |

Indexes:
- `ix_agent_runs_status_started` (partial: status IN queued/active)
- `ix_agent_runs_user_completed` (partial: completed_at IS NOT NULL)
- `ix_agent_runs_session_started`
- `ix_agent_runs_active` (partial: deleted_at IS NULL)

### `agent_run_events` (append-only timeline)

| Column        | Type                | Notes                                          |
|---------------|---------------------|------------------------------------------------|
| `id`          | BIGINT IDENTITY PK  |                                                |
| `run_id`      | VARCHAR(64) FK CASCADE | references `agent_runs.id`                  |
| `sequence`    | INTEGER             | per-run monotonic, starts at 1                 |
| `event_type`  | VARCHAR(32)         | `lifecycle | turn | llm | tool | sub_agent | compaction | streaming` |
| `data`        | JSONB               | event-specific payload                         |
| `elapsed_ms`  | BIGINT              | ms since `agent_runs.started_at`               |
| `created_at`  | TIMESTAMPTZ         |                                                |

Indexes:
- `uq_agent_run_events_run_sequence` (UNIQUE)
- `ix_agent_run_events_run_created`
- `ix_agent_run_events_type_created`
- `ix_agent_run_events_data_gin` (GIN, jsonb_path_ops)

### `gateway_usage_events` (partitioned monthly)

Parent table `PARTITION BY RANGE (created_at)`. The migration
pre-creates current month + 12 ahead. The helper function
`gateway_create_usage_partition(DATE)` extends the rolling window;
a daily cron in the gateway service calls it for `now() + 60 days`.

Per-partition indexes (auto-created by `gateway_create_usage_partition`):
- `ix_<partition>_user_created` on `(user_id, created_at)`
- `ix_<partition>_run` on `(run_id) WHERE run_id IS NOT NULL`

### Dashboard support views

`v_agents_running` - every active or queued run plus elapsed time,
event count, current model. Used by the dashboard's "Live agents" card.

`v_agents_top_cost_7d` - top-50 users by spend in the rolling 7
days. Used by the admin "billing leaderboard" page.

`v_user_quota_state` - effective quota_def per user (override
falling back to plan), current counters as a JSONB map, and current
block status. Used everywhere the gateway needs "is this user over
quota".

`v_usage_top_users_month` - top-50 users by spend in the current
calendar month partition.

## Operational guides

### Applying the migrations

```bash
# 0. set DATABASE_URL to live Neon (read from your daemon's config)
export DIGITORN_GATEWAY_DATABASE_URL='postgresql+psycopg2://…'
export DIGITORN_DATABASE__URL='postgresql+asyncpg://…'

# 1. apply daemon sprints A–H + F + G + H
alembic -c alembic.ini upgrade head

# 2. apply gateway sprint E
cd packages/gateway
alembic -c alembic.ini upgrade head
```

Each sprint is independently rollback-safe except Sprint A, which
is intentionally one-way (the dropped tables held no live data, and
narrowing `history_log.id` back to INT4 risks overflow).

### Adding a new partition for `history_log` or `gateway_usage_events`

```sql
-- one-off
SELECT digitorn_create_history_partition(DATE '2026-12-01');
SELECT gateway_create_usage_partition(DATE '2026-12-01');
```

A daily cron in the daemon (and gateway) advances the rolling window
to `now() + 60 days`. Implementation lives in
`packages/digitorn/core/cron/partition_keeper.py` (TBD - sprint
follow-up; the SQL helpers above are already in place).

### Dropping an old partition (retention)

Each `audit_actions_catalog` row carries a `retention_days` value.
The cleanup job lives in `packages/digitorn/core/cron/retention_keeper.py`
(TBD): for each partition older than `MAX(retention_days)`, drop
the partition - constant-time, no `DELETE` scan.

```sql
DROP TABLE history_log_2024_03;  -- removes ~10M rows in <1s
```

### Wiring RLS in the daemon

Migration 0007 defines the policies but the daemon currently
connects as the table owner (which BYPASSES RLS). To activate the
guard:

1. As DB superuser, create an application role:

   ```sql
   CREATE ROLE digitorn_app NOLOGIN NOBYPASSRLS;
   GRANT ALL ON ALL TABLES IN SCHEMA public TO digitorn_app;
   GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO digitorn_app;
   CREATE USER digitorn_app_user WITH LOGIN PASSWORD '…' IN ROLE digitorn_app;
   ```

2. Update `DATABASE_URL` to use `digitorn_app_user`.
3. Wire the FastAPI middleware that runs before each request:

   ```python
   await conn.execute(
       "SELECT set_config('digitorn.current_user_id', $1, true)",
       jwt.user_id,
   )
   if jwt.is_admin:
       await conn.execute(
           "SELECT set_config('digitorn.is_admin', 'true', true)"
       )
   ```

   The `true` argument makes the setting `LOCAL` (transaction
   scope), so a pooled connection can't leak across requests.

### Reading the cost trigger

Every UPDATE of `agent_runs.cost_breakdown` triggers
`agent_runs_recompute_cost()`, which sums every leaf
`cost_breakdown.<provider>.total_usd` and writes the total into
`agent_runs.total_cost_usd`. To skip the trigger (rare, e.g. bulk
backfills), update `total_cost_usd` directly without touching
`cost_breakdown`. Same pattern on `gateway_usage_events`.

## Future work (not in this sprint set)

These were considered and deferred:

  * `agent_runs.parent_run_id` is already in place; the recursive
    "agent tree" view (`v_agent_tree`) for the dashboard's nested
    drill-down is a follow-up.
  * Read replicas: Postgres 16 logical replication on the
    `gateway_usage_events` partitions is the right scaling move once
    we exceed ~10M events/month.
  * Materialised views for the "billing leaderboard" pages once the
    underlying queries hit production load - the existing views are
    fast for current scale (under 10k users).
  * Drop `inbox_devices` / `inbox_notification_prefs` once every
    write goes through `user_devices` (one full release cycle of
    dual-write before drop).
  * Drop `action_executions.agent_pk` once every emitter writes
    `agent_run_id` instead.
  * Per-app retention policy on `history_log` (currently the
    retention is global via `audit_actions_catalog.retention_days`).

## Reference files

  * Daemon migrations: `packages/digitorn/core/migrations/versions/`
  * Gateway migrations: `packages/gateway/alembic/versions/`
  * ORM models (daemon): `packages/digitorn/core/models.py`
  * ORM models (gateway): `packages/gateway/src/digitorn_gateway/models_db.py`
  * Memory pointer (this plan): `~/.claude/projects/c--Users-ASUS-Documents-digitorn-bridge/memory/project_db_v2_plan.md`
