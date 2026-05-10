"""Quota engine.

Two guarantees:

1. The pre-call check is **always** O(1) memory access. No DB touch,
   no network. A request that crosses the limit is rejected in
   nanoseconds via a sticky in-memory block flag.

2. The post-call record is **never** awaited on the hot path the
   user perceives. We update an in-memory counter (sub-µs) and let
   a background task flush to Postgres every `flush_interval_seconds`.

Architecture:

    +------------------------+
    |  pre_call(user_id)     |  ← O(1) dict lookup. Returns block reason or None.
    |    └─ checks _blocks   |
    +------------------------+

    +------------------------+
    |  record(user_id, ...)  |  ← O(M) where M = #metrics for this user's plan.
    |    └─ increments       |     M is at most ~6, all dict updates.
    |       _counters[ukey]  |     Crosses limits → sets _blocks[user_id].
    |    └─ enqueue dirty    |
    +------------------------+

    +------------------------+
    |  Background flush      |  ← every flush_interval_seconds:
    |    └─ snapshot dirty   |     - copy dirty counter map
    |    └─ UPSERT Postgres  |     - reset dirty marker
    +------------------------+

    +------------------------+
    |  Recovery at boot      |  ← read non-expired counters + blocks from
    |                        |     Postgres into the in-memory dicts.
    +------------------------+

The plan resolution (user_id -> Plan -> QuotaDefinition) is owned by
`plans.PlanRegistry`. The engine just asks "what's the rule for
(user_id, metric)?" and gets back a list of (window_key, limit) tuples.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from digitorn_gateway.models_db import QuotaBlock, QuotaCounter
from digitorn_gateway.quota_schema import (
    METRICS,
    MetricQuota,
    QuotaDefinition,
    QuotaRule,
    window_to_seconds,
)

logger = logging.getLogger(__name__)


# ── Records ────────────────────────────────────────────────────────


@dataclass(slots=True)
class UsageRecord:
    """One LLM call's worth of usage. Built by `llm_call.dispatch` and
    handed to the engine via `record()` post-LLM."""

    user_id: str
    model_alias: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    success: bool = True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class BlockInfo:
    """In-memory representation of a sticky block."""

    user_id: str
    blocked_until: float          # monotonic timestamp
    blocked_until_dt: datetime    # absolute timestamp for client display
    reason: str
    metric: str
    window: str
    limit_value: float
    actual_value: float


# ── Window math ────────────────────────────────────────────────────


def _bucket_start(now: datetime, reset: str, window_name: str) -> datetime:
    """Compute the START timestamp of the bucket the given `now` falls
    into, for the given reset strategy + window name. The bucket key
    used to identify a counter row is "reset:start_iso".
    """
    if reset == "fixed_daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if reset == "fixed_weekly":
        # ISO week starts Monday. weekday() = 0 for Monday.
        days_since_monday = now.weekday()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight - timedelta(days=days_since_monday)
    if reset == "fixed_monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if reset == "rolling_from_first":
        # Bucket starts at the first hit; no clean alignment to the
        # wall clock. Use a stable per-(user,metric,window) key
        # constructed by the caller and let the engine track
        # `started_at` separately.
        return now
    # "fixed" with a free-form window name (per_minute, per_hour, custom)
    if window_name == "per_minute":
        return now.replace(second=0, microsecond=0)
    if window_name == "per_hour":
        return now.replace(minute=0, second=0, microsecond=0)
    if window_name == "per_day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    # custom window like "5h" / "30m" - epoch-aligned bucket.
    seconds = window_to_seconds(window_name)
    epoch = int(now.timestamp())
    bucket_epoch = (epoch // seconds) * seconds
    return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)


def _bucket_end(start: datetime, reset: str, window_name: str) -> datetime:
    if reset == "fixed_daily":
        return start + timedelta(days=1)
    if reset == "fixed_weekly":
        return start + timedelta(weeks=1)
    if reset == "fixed_monthly":
        # Calendar month - simple but accurate enough.
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)
    if reset == "rolling_from_first":
        seconds = window_to_seconds(window_name)
        return start + timedelta(seconds=seconds)
    if window_name == "per_minute":
        return start + timedelta(minutes=1)
    if window_name == "per_hour":
        return start + timedelta(hours=1)
    if window_name == "per_day":
        return start + timedelta(days=1)
    return start + timedelta(seconds=window_to_seconds(window_name))


def _bucket_key(metric: str, window_name: str, start: datetime) -> str:
    """Stable key for the counter map: `metric|window|start_iso`."""
    return f"{metric}|{window_name}|{start.replace(microsecond=0).isoformat()}"


def _extra_for(
    extra: QuotaDefinition | None, metric: str, window: str,
) -> float:
    """Look up the overage allowance for ``(metric, window)``. Returns
    0.0 when nothing is configured (the common case)."""
    if extra is None:
        return 0.0
    mq = extra.metric_for_model(metric, None)
    if mq is None:
        return 0.0
    for w_name, rule in mq.rules():
        if w_name == window:
            return float(rule.limit)
    return 0.0


# ── Engine ─────────────────────────────────────────────────────────


class QuotaEngine:
    """In-memory quota tracker with periodic Postgres flush.

    Single instance per gateway process. Thread-/coroutine-safe via the
    GIL + the fact that all mutations happen on the asyncio loop.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        plan_registry: Any,  # plans.PlanRegistry, avoid circular import
        flush_interval_seconds: int = 10,
        redis_coordinator: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._plans = plan_registry
        self._flush_interval_s = flush_interval_seconds

        # In-memory counters. Outer key = user_id, inner key = bucket key.
        # Each value is a dict that also carries `_started_at` for rolling
        # windows and `_reset_at` for cheap eviction at flush time.
        self._counters: dict[str, dict[str, dict[str, Any]]] = {}

        # Sticky block flags. Reading is sub-µs.
        self._blocks: dict[str, BlockInfo] = {}

        # Bucket keys touched since last flush. Set membership + iter.
        self._dirty: set[tuple[str, str]] = set()

        # Background task handles; None until start().
        self._flush_task: asyncio.Task | None = None
        self._supervisor_task: asyncio.Task | None = None
        self._stopped = False

        # Supervisor scheduling state (estimation-driven block decisions).
        # ``_supervisor_dirty`` collects user_ids touched by record(); the
        # supervisor pops one at a time. ``_next_check_at`` caches the
        # per-user "earliest-check" timestamp so a user well below their
        # limit doesn't get re-evaluated more often than necessary.
        # ``_last_check_state`` stores the (value, monotonic_ts) at the
        # last check per (user_id, metric, window) so the supervisor
        # can derive a burn rate and pick a smart next interval.
        self._supervisor_dirty: set[str] = set()
        self._next_check_at: dict[str, float] = {}
        self._last_check_state: dict[tuple[str, str, str], tuple[float, float]] = {}
        # Tunable: how often the supervisor wakes to drain ``_supervisor_dirty``.
        # 1s gives a worst-case 1s lag between the user crossing their
        # limit and the sticky block being set. The user explicitly
        # accepted "un peu de dépassement" as the trade-off.
        self._supervisor_tick_s: float = 1.0
        # Resilience caps: at very high concurrency (100k+ users) the
        # dirty set could grow faster than the supervisor drains it.
        # Hard cap prevents unbounded memory growth - past this we
        # drop the OLDEST entries (an unprocessed user just gets
        # re-dirtied on its next request, no correctness loss).
        # ``_max_supervisor_dirty=0`` disables the cap (legacy).
        self._max_supervisor_dirty: int = 100_000
        # Periodic GC tick for expired blocks: blocks have a wall-clock
        # ``blocked_until`` that we already check on read, but the dict
        # entries linger until the next read for that user. With many
        # users, that pile-up is wasteful. The GC sweep below drops
        # entries whose monotonic deadline has passed.
        self._last_blocks_gc_at: float = time.monotonic()

        # Optional Redis coordinator for cross-worker truth. When None,
        # the engine runs in legacy single-process mode. When set, the
        # post-call ``record()`` fans out atomic INCR + Pub/Sub block
        # broadcasts in the background, while the hot path (is_blocked)
        # keeps reading the local in-memory dict for sub-µs latency.
        self._redis: Any | None = redis_coordinator

        # Strong references to in-flight reconciliation tasks so the
        # asyncio GC doesn't drop them mid-flight. Tasks self-discard
        # via ``add_done_callback``.
        self._reconcile_tasks: set[asyncio.Task] = set()

    # ── Pre-call ───────────────────────────────────────────────

    def is_blocked(self, user_id: str) -> tuple[bool, BlockInfo | None]:
        """O(1). Returns (True, info) if the user has a live block.
        Stale blocks (`blocked_until` past) are evicted opportunistically.
        """
        info = self._blocks.get(user_id)
        if info is None:
            return False, None
        if info.blocked_until <= time.monotonic():
            # Expired - clean and let the caller through.
            self._blocks.pop(user_id, None)
            return False, None
        return True, info

    # ── Post-call ──────────────────────────────────────────────

    async def record(self, record: UsageRecord) -> None:
        """O(M). Increment every counter touched by this call.

        **Hot-path discipline**: this method only mutates in-memory
        counters and marks them dirty for the flush task. It does NOT
        decide whether to block the user - that's the
        ``QuotaSupervisor``'s job, running on its own background loop
        with an estimation-driven check schedule. A user crossing
        their limit between two supervisor passes can squeeze a few
        extra requests through; we accept that to keep ``record()``
        bounded and predictable (the user's stated trade-off:
        "c'est pas grave si l'utilisateur depasse un peu").

        Called from a FastAPI BackgroundTask, so it never sits on the
        request's response path.
        """
        plan_def = await self._plans.resolve(record.user_id)
        if plan_def is None:
            # No plan / quota disabled for this user. No tracking, no block.
            return

        now = datetime.now(timezone.utc)
        deltas: dict[str, float] = {
            "requests": 1.0,
            "messages": 1.0,
            "tokens_input": float(record.input_tokens),
            "tokens_output": float(record.output_tokens),
            "tokens_total": float(record.total_tokens),
            "cost_usd": float(record.cost_usd),
        }

        redis_alive = bool(self._redis and self._redis.alive)
        redis_targets: list[tuple[str, str, float, datetime, datetime, float]] = []

        for metric in METRICS:
            metric_quota: MetricQuota | None = plan_def.metric_for_model(
                metric, record.model_alias,
            )
            if metric_quota is None:
                continue
            delta = deltas.get(metric, 0.0)
            if delta == 0.0:
                continue
            for window_name, rule in metric_quota.rules():
                start = _bucket_start(now, rule.reset, window_name)
                end = _bucket_end(start, rule.reset, window_name)
                bucket = self._touch_bucket(
                    record.user_id, metric, window_name, start, end,
                )
                bucket["value"] = float(bucket.get("value", 0.0)) + delta
                self._dirty.add((record.user_id, bucket["key"]))
                if redis_alive:
                    redis_targets.append(
                        (metric, window_name, delta, start, end, rule.limit),
                    )

        # Hand the user_id to the supervisor for next-pass evaluation.
        # The supervisor uses `next_check_at` heuristics to decide when
        # to actually walk the buckets and set/clear blocks.
        self._supervisor_dirty.add(record.user_id)

        if redis_alive and redis_targets:
            task = asyncio.create_task(
                self._redis_reconcile(
                    record.user_id, plan_def, redis_targets,
                ),
                name="quota-redis-reconcile",
            )
            self._reconcile_tasks.add(task)
            task.add_done_callback(self._reconcile_tasks.discard)

    def _touch_bucket(
        self,
        user_id: str,
        metric: str,
        window_name: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        user_buckets = self._counters.setdefault(user_id, {})
        key = _bucket_key(metric, window_name, start)
        bucket = user_buckets.get(key)
        if bucket is None:
            bucket = {
                "key": key,
                "metric": metric,
                "window": window_name,
                "start": start,
                "end": end,
                "value": 0.0,
            }
            user_buckets[key] = bucket
        return bucket

    def _block_expiry_for(
        self, window_name: str, plan_def: QuotaDefinition, metric: str,
    ) -> tuple[float, datetime]:
        """When the block lifts. Equals the end of the offending bucket
        (so a 'per_day' overflow blocks until UTC midnight)."""
        metric_quota = plan_def.metric_for_model(metric, None)
        if metric_quota is None:
            # Defensive - shouldn't happen if we got here from record()
            return time.monotonic() + 60.0, datetime.now(timezone.utc) + timedelta(minutes=1)
        for w_name, rule in metric_quota.rules():
            if w_name == window_name:
                now = datetime.now(timezone.utc)
                start = _bucket_start(now, rule.reset, window_name)
                end = _bucket_end(start, rule.reset, window_name)
                seconds_until_end = (end - now).total_seconds()
                return time.monotonic() + max(seconds_until_end, 1.0), end
        return time.monotonic() + 60.0, datetime.now(timezone.utc) + timedelta(minutes=1)

    async def _redis_reconcile(
        self,
        user_id: str,
        plan_def: QuotaDefinition,
        targets: list[tuple[str, str, float, datetime, datetime, float]],
    ) -> None:
        """Atomically INCR every touched bucket on Redis and check
        cluster-wide overflow. When the Redis tally crosses a limit
        (and the local dict didn't already block), promote the local
        block AND broadcast it via Pub/Sub so concurrent workers
        catch up immediately.

        Runs as a background task spawned by ``record()`` - never on
        the request hot path. Redis hiccups are swallowed; the local
        counter remains the authoritative fallback for that worker.
        """
        for metric, window, delta, start, end, limit_v in targets:
            try:
                ttl = max(int((end - start).total_seconds()), 60)
                bucket_epoch = int(start.timestamp())
                new_total = await self._redis.increment(
                    user_id=user_id, metric=metric, window=window,
                    bucket_start_epoch=bucket_epoch, delta=delta,
                    ttl_seconds=ttl,
                )
            except Exception as exc:
                logger.debug(
                    "redis_reconcile_inc_failed user=%s metric=%s err=%s",
                    user_id, metric, exc,
                )
                continue
            if new_total is None:
                continue
            # Patch the local bucket so ``snapshot()`` reflects the
            # cluster-wide truth (otherwise /v1/quota/me would only
            # show this worker's slice). Cheap: O(1) dict write.
            try:
                user_buckets = self._counters.setdefault(user_id, {})
                key = _bucket_key(metric, window, start)
                bucket = user_buckets.get(key)
                if bucket is not None:
                    bucket["value"] = max(
                        float(bucket.get("value", 0.0)),
                        float(new_total),
                    )
            except Exception:
                pass
            if new_total > limit_v and user_id not in self._blocks:
                await self._set_block(
                    user_id=user_id,
                    metric=metric,
                    window=window,
                    limit_value=limit_v,
                    actual_value=new_total,
                    blocked_until=self._block_expiry_for(
                        window, plan_def, metric,
                    ),
                    publish=True,
                )

    async def _on_remote_block(self, payload: dict[str, Any]) -> None:
        """Handler for Pub/Sub block broadcasts from peer workers.

        Re-derives ``blocked_until`` against this worker's monotonic
        clock from the wall-clock ``blocked_until_dt_iso`` field. Skips
        broadcasts about users we already block locally - the dict
        write is idempotent but the log line is noise.
        """
        try:
            user_id = payload.get("user_id") or ""
            if not user_id:
                return
            iso = payload.get("blocked_until_dt_iso")
            if not iso:
                return
            from datetime import datetime as _dt
            try:
                until_dt = _dt.fromisoformat(iso)
            except Exception:
                return
            now_dt = datetime.now(timezone.utc)
            seconds_until_end = max((until_dt - now_dt).total_seconds(), 1.0)
            self._blocks[user_id] = BlockInfo(
                user_id=user_id,
                blocked_until=time.monotonic() + seconds_until_end,
                blocked_until_dt=until_dt,
                reason=payload.get("reason", "quota_exceeded"),
                metric=payload.get("metric", ""),
                window=payload.get("window", ""),
                limit_value=float(payload.get("limit_value") or 0.0),
                actual_value=float(payload.get("actual_value") or 0.0),
            )
            logger.info(
                "quota_block_received_from_peer user=%s metric=%s window=%s",
                user_id, payload.get("metric"), payload.get("window"),
            )
        except Exception as exc:
            logger.warning("quota_remote_block_apply_failed err=%s", exc)

    async def _set_block(
        self,
        *,
        user_id: str,
        metric: str,
        window: str,
        limit_value: float,
        actual_value: float,
        blocked_until: tuple[float, datetime],
        publish: bool = True,
    ) -> None:
        mono, dt = blocked_until
        info = BlockInfo(
            user_id=user_id,
            blocked_until=mono,
            blocked_until_dt=dt,
            reason=f"{metric}_quota_exceeded",
            metric=metric,
            window=window,
            limit_value=limit_value,
            actual_value=actual_value,
        )
        self._blocks[user_id] = info
        # Persist immediately - block state must survive a restart.
        try:
            async with self._session_factory() as db:
                stmt_values = {
                    "user_id": user_id,
                    "blocked_until": dt,
                    "reason": info.reason,
                    "metric": metric,
                    "window": window,
                    "limit_value": limit_value,
                    "actual_value": actual_value,
                }
                stmt = _upsert_block(stmt_values)
                await db.execute(stmt)
                await db.commit()
        except Exception as exc:
            logger.warning(
                "quota_block_persist_failed user=%s err=%s",
                user_id, exc,
            )
        # Cross-worker broadcast. Best-effort - if Redis is down or
        # this worker is in single-process mode, the block still works
        # locally; peer workers just won't sync until their own next
        # increment surfaces the same overflow.
        if publish and self._redis is not None and self._redis.alive:
            try:
                await self._redis.publish_block(info)
            except Exception as exc:
                logger.debug("quota_block_broadcast_failed: %s", exc)
        # Postgres NOTIFY fallback when Redis isn't configured. Peer
        # workers' ``cluster_sync`` listener handles this channel by
        # calling ``refresh_blocks_from_db`` so the block lands on
        # every worker within milliseconds even without Redis.
        if publish:
            try:
                from digitorn_gateway.cluster_sync import (
                    notify as _notify, CHANNEL_QUOTA_BLOCKS,
                )
                await _notify(CHANNEL_QUOTA_BLOCKS, user_id)
            except Exception as exc:
                logger.debug("quota_block_pg_notify_failed: %s", exc)

    # ── Read paths (for /v1/quota/me + admin) ──────────────────

    def snapshot(self, user_id: str) -> dict[str, Any]:
        """Build a JSON-friendly summary of the user's current usage.
        Pure in-memory read. The flush task is what made the data fresh.
        """
        buckets = self._counters.get(user_id, {})
        block = self._blocks.get(user_id)
        return {
            "user_id": user_id,
            "blocked": block is not None and block.blocked_until > time.monotonic(),
            "block_info": (
                {
                    "until": block.blocked_until_dt.isoformat(),
                    "reason": block.reason,
                    "metric": block.metric,
                    "window": block.window,
                    "limit": block.limit_value,
                    "actual": block.actual_value,
                }
                if block else None
            ),
            "counters": [
                {
                    "metric": b["metric"],
                    "window": b["window"],
                    "value": b["value"],
                    "bucket_start": b["start"].isoformat(),
                    "bucket_end": b["end"].isoformat(),
                }
                for b in buckets.values()
            ],
        }

    # ── Background flush ───────────────────────────────────────

    def start(self, *, start_supervisor: bool = True) -> None:
        """Start background tasks.

        ``start_supervisor`` is False when the lifespan loses the
        cluster-wide ``quota_supervisor`` advisory lock: another
        worker is the leader, so this worker just runs the per-process
        ``_flush_task`` and skips the supervisor loop. When the leader
        dies, the leader-election loop calls ``start_supervisor()``
        on the surviving winner.
        """
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(
                self._flush_loop(), name="quota-flush-loop",
            )
        if start_supervisor and self._supervisor_task is None:
            self._supervisor_task = asyncio.create_task(
                self._supervisor_loop(), name="quota-supervisor-loop",
            )

    def start_supervisor(self) -> None:
        """Start ONLY the supervisor task. Called by the leader-election
        loop when this worker just became the leader."""
        if self._supervisor_task is None:
            self._supervisor_task = asyncio.create_task(
                self._supervisor_loop(), name="quota-supervisor-loop",
            )

    async def stop_supervisor(self) -> None:
        """Stop ONLY the supervisor task. Called when this worker
        loses the leader role (rare but possible after lock recovery)."""
        t = self._supervisor_task
        if t is None:
            return
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        self._supervisor_task = None

    async def stop(self) -> None:
        self._stopped = True
        for task_name in ("_flush_task", "_supervisor_task"):
            t = getattr(self, task_name)
            if t is not None:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
                setattr(self, task_name, None)
        # Final flush so the last few seconds of writes don't vanish.
        try:
            await self._flush_once()
        except Exception as exc:
            logger.warning("quota_final_flush_failed: %s", exc)

    async def _flush_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._flush_interval_s)
                await self._flush_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("quota_flush_iteration_failed: %s", exc)

    # ── Supervisor (estimation-driven block decisions) ─────────────

    def _gc_expired_blocks(self) -> int:
        """Drop in-memory blocks whose wall-clock deadline has passed.
        ``is_blocked()`` already does this lazily on read, but at high
        user count the dict can accumulate stale entries between reads.
        Called periodically from the supervisor loop. Returns the number
        of entries dropped."""
        if not self._blocks:
            return 0
        now_mono = time.monotonic()
        stale = [u for u, info in self._blocks.items()
                 if info.blocked_until <= now_mono]
        for u in stale:
            self._blocks.pop(u, None)
        return len(stale)

    def _enforce_supervisor_dirty_cap(self) -> int:
        """Trim ``_supervisor_dirty`` to ``_max_supervisor_dirty`` so a
        runaway supervisor backlog never explodes memory. Drops the
        oldest (= insertion-order via Python set ordering) entries.
        Dropped users will be re-dirtied on their next ``record()``
        call - no quota correctness loss. Returns trim count."""
        cap = self._max_supervisor_dirty
        if cap <= 0:
            return 0
        n = len(self._supervisor_dirty)
        if n <= cap:
            return 0
        excess = n - cap
        # Set iteration order in CPython 3.7+ matches insertion order
        # closely enough for "oldest first" trimming.
        to_drop = list(self._supervisor_dirty)[:excess]
        for u in to_drop:
            self._supervisor_dirty.discard(u)
        return excess

    async def _supervisor_loop(self) -> None:
        """Drain ``_supervisor_dirty`` periodically, decide blocks.

        Single asyncio task. Wakes every ``_supervisor_tick_s`` (1s by
        default), iterates every dirty user_id, and for each one either
            (a) skips - the user is below their next_check_at horizon, or
            (b) runs ``_supervisor_check_user`` which reads counters,
                compares against plan_limit + extra_usage, and sets a
                sticky block when overflowed.
        After each check we recompute next_check_at from the burn rate
        so a user well below their limit gets re-evaluated less often.
        """
        while not self._stopped:
            try:
                await asyncio.sleep(self._supervisor_tick_s)
                # Memory hygiene every 60s: drop expired blocks, trim
                # the dirty backlog. Both are pure-memory ops; sub-ms
                # at 100k users.
                now_mono = time.monotonic()
                if now_mono - self._last_blocks_gc_at > 60.0:
                    dropped = self._gc_expired_blocks()
                    trimmed = self._enforce_supervisor_dirty_cap()
                    if dropped or trimmed:
                        logger.debug(
                            "quota_gc_swept blocks_dropped=%d dirty_trimmed=%d",
                            dropped, trimmed,
                        )
                    self._last_blocks_gc_at = now_mono
                if not self._supervisor_dirty:
                    continue
                # Snapshot so concurrent record() calls don't race us.
                snapshot = list(self._supervisor_dirty)
                self._supervisor_dirty.clear()
                now_mono = time.monotonic()
                for user_id in snapshot:
                    next_at = self._next_check_at.get(user_id, 0.0)
                    if now_mono < next_at:
                        # Re-queue: this user got dirty but their estimated
                        # safe horizon hasn't passed. Will be re-evaluated
                        # on a future tick.
                        self._supervisor_dirty.add(user_id)
                        continue
                    try:
                        await self._supervisor_check_user(user_id)
                    except Exception as exc:
                        logger.warning(
                            "supervisor_check_failed user=%s err=%s",
                            user_id, exc,
                        )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("supervisor_iteration_failed: %s", exc)

    async def _supervisor_check_user(self, user_id: str) -> None:
        """Walk the user's counters once; set a sticky block when any
        ``actual > plan_limit + extra_usage``. Recompute the per-user
        ``next_check_at`` based on the worst-case fill ratio so heavy
        users get checked more often than idle ones.
        """
        # Already blocked - nothing to compute, the block expires on its
        # own clock. Push the next check past the block expiry.
        info = self._blocks.get(user_id)
        if info is not None and info.blocked_until > time.monotonic():
            self._next_check_at[user_id] = info.blocked_until + 1.0
            return

        plan_def = await self._plans.resolve(user_id)
        if plan_def is None:
            self._next_check_at[user_id] = time.monotonic() + 60.0
            return
        extra = self._plans.resolve_extra_usage(user_id)

        user_buckets = self._counters.get(user_id) or {}
        if not user_buckets:
            self._next_check_at[user_id] = time.monotonic() + 60.0
            return

        # Walk every active bucket; compare against effective limit
        # (plan + extra). For each bucket, derive a burn rate from the
        # previous check's value and pick the tightest next-check
        # interval. The min across buckets becomes the user's
        # next_check_at.
        now_mono = time.monotonic()
        next_intervals: list[float] = []
        first_overflow: tuple[str, str, float, float] | None = None
        for _key, bucket in user_buckets.items():
            metric = bucket["metric"]
            window = bucket["window"]
            value = float(bucket.get("value", 0.0))
            mq = plan_def.metric_for_model(metric, None)
            if mq is None:
                continue
            for w_name, rule in mq.rules():
                if w_name != window:
                    continue
                ex = _extra_for(extra, metric, window)
                effective_limit = float(rule.limit) + ex
                if effective_limit <= 0:
                    continue
                if value > effective_limit:
                    if first_overflow is None:
                        first_overflow = (metric, window, effective_limit, value)
                    break
                state_key = (user_id, metric, window)
                prev = self._last_check_state.get(state_key)
                self._last_check_state[state_key] = (value, now_mono)
                if prev is None:
                    # First check for this bucket - probe again in 2s
                    # to get a usable burn rate measurement.
                    next_intervals.append(2.0)
                    break
                prev_value, prev_ts = prev
                dt = max(now_mono - prev_ts, 0.001)
                burn = max(value - prev_value, 0.0) / dt
                if burn <= 0.0:
                    next_intervals.append(60.0)
                    break
                # ETA at current burn rate, halved so the block fires
                # BEFORE the user is wildly over the ceiling.
                eta = max(effective_limit - value, 0.0) / burn
                next_intervals.append(max(min(eta * 0.5, 30.0), 1.0))
                break

        if first_overflow is not None:
            metric, window, limit_v, actual_v = first_overflow
            await self._set_block(
                user_id=user_id, metric=metric, window=window,
                limit_value=limit_v, actual_value=actual_v,
                blocked_until=self._block_expiry_for(window, plan_def, metric),
            )
            # Block is set; no further checks needed until it expires.
            self._next_check_at[user_id] = info.blocked_until + 1.0 if info else (
                time.monotonic() + 60.0
            )
            return

        # The next check fires at the tightest of all per-bucket ETAs,
        # capped to 60s for idle users (no buckets at all).
        interval = min(next_intervals) if next_intervals else 60.0
        self._next_check_at[user_id] = time.monotonic() + interval

    async def _flush_once(self) -> int:
        """UPSERT every dirty counter into Postgres. Returns the number
        of rows touched. Snapshot-and-clear pattern so concurrent
        record() calls don't race the flush.
        """
        if not self._dirty:
            return 0
        snapshot = list(self._dirty)
        self._dirty.clear()

        rows: list[dict[str, Any]] = []
        for user_id, key in snapshot:
            buckets = self._counters.get(user_id) or {}
            bucket = buckets.get(key)
            if bucket is None:
                continue
            rows.append({
                "user_id": user_id,
                "metric": bucket["metric"],
                "window_key": key,
                "value": float(bucket["value"]),
                "reset_at": bucket["end"],
            })

        if not rows:
            return 0

        try:
            async with self._session_factory() as db:
                for row in rows:
                    stmt = _upsert_counter(row)
                    await db.execute(stmt)
                await db.commit()
        except Exception as exc:
            # Re-mark dirty so the next iteration retries.
            for user_id, key in snapshot:
                self._dirty.add((user_id, key))
            logger.warning(
                "quota_flush_db_failed rows=%d err=%s (re-queued)",
                len(rows), exc,
            )
            return 0

        return len(rows)

    # ── Recovery at boot ───────────────────────────────────────

    async def recover_from_db(self) -> None:
        """Read non-expired counters and blocks from Postgres into
        memory. Called from `main.lifespan` after the engine is built
        but before the flush loop is started.
        """
        now = datetime.now(timezone.utc)
        async with self._session_factory() as db:
            counter_rows = (
                await db.execute(
                    select(QuotaCounter).where(QuotaCounter.reset_at > now),
                )
            ).scalars().all()
            for row in counter_rows:
                user_buckets = self._counters.setdefault(row.user_id, {})
                user_buckets[row.window_key] = {
                    "key": row.window_key,
                    "metric": row.metric,
                    "window": row.window_key.split("|")[1] if "|" in row.window_key else row.window_key,
                    "start": _bucket_start_from_key(row.window_key) or now,
                    "end": row.reset_at,
                    "value": float(row.value),
                }
            block_rows = (
                await db.execute(
                    select(QuotaBlock).where(QuotaBlock.blocked_until > now),
                )
            ).scalars().all()
            for b in block_rows:
                seconds_until_end = (b.blocked_until - now).total_seconds()
                self._blocks[b.user_id] = BlockInfo(
                    user_id=b.user_id,
                    blocked_until=time.monotonic() + max(seconds_until_end, 1.0),
                    blocked_until_dt=b.blocked_until,
                    reason=b.reason,
                    metric=b.metric,
                    window=b.window,
                    limit_value=b.limit_value,
                    actual_value=b.actual_value,
                )
            # Garbage-collect expired counter rows and block rows.
            await db.execute(delete(QuotaCounter).where(QuotaCounter.reset_at <= now))
            await db.execute(delete(QuotaBlock).where(QuotaBlock.blocked_until <= now))
            await db.commit()

        logger.info(
            "quota_engine_recovered counters=%d blocks=%d",
            sum(len(b) for b in self._counters.values()),
            len(self._blocks),
        )

    async def refresh_blocks_from_db(self) -> int:
        """Re-read live ``gateway_quota_blocks`` rows and overwrite the
        in-memory ``_blocks`` dict. Used by the cross-worker NOTIFY
        listener: when a peer set a block, this worker pulls the row
        and updates its hot-path snapshot. Returns the number of
        blocks loaded. Never raises."""
        try:
            now = datetime.now(timezone.utc)
            async with self._session_factory() as db:
                rows = (
                    await db.execute(
                        select(QuotaBlock).where(QuotaBlock.blocked_until > now),
                    )
                ).scalars().all()
            new_blocks: dict[str, BlockInfo] = {}
            for b in rows:
                seconds_until_end = (b.blocked_until - now).total_seconds()
                new_blocks[b.user_id] = BlockInfo(
                    user_id=b.user_id,
                    blocked_until=time.monotonic() + max(seconds_until_end, 1.0),
                    blocked_until_dt=b.blocked_until,
                    reason=b.reason,
                    metric=b.metric,
                    window=b.window,
                    limit_value=b.limit_value,
                    actual_value=b.actual_value,
                )
            # Replace atomically. Entries that were dropped by the leader
            # (cleared block, expired naturally) disappear from this
            # worker's view too.
            self._blocks = new_blocks
            logger.debug("quota_blocks_refreshed_from_db count=%d", len(new_blocks))
            return len(new_blocks)
        except Exception as exc:
            logger.warning("quota_blocks_refresh_failed: %s", exc)
            return 0

    # ── Admin helpers ──────────────────────────────────────────

    async def reset_user(self, user_id: str) -> None:
        """Wipe a user's in-memory + persisted counters + block. Used
        by the admin "reset quota" route.
        """
        self._counters.pop(user_id, None)
        self._blocks.pop(user_id, None)
        # Drop any dirty markers tied to this user.
        self._dirty = {(u, k) for (u, k) in self._dirty if u != user_id}
        # Same for the supervisor scheduling state - otherwise a user
        # that was blocked then reset keeps a future ``next_check_at``
        # and the supervisor skips them for up to a window's worth of
        # time. Clearing here makes the next record() trigger an
        # immediate supervisor pass.
        self._supervisor_dirty.discard(user_id)
        self._next_check_at.pop(user_id, None)
        for k in [k for k in self._last_check_state if k[0] == user_id]:
            self._last_check_state.pop(k, None)
        try:
            async with self._session_factory() as db:
                await db.execute(
                    delete(QuotaCounter).where(QuotaCounter.user_id == user_id),
                )
                await db.execute(
                    delete(QuotaBlock).where(QuotaBlock.user_id == user_id),
                )
                await db.commit()
        except Exception as exc:
            logger.warning(
                "quota_reset_user_db_failed user=%s err=%s", user_id, exc,
            )
        # Cross-worker: tell every peer to drop this user's block too.
        try:
            from digitorn_gateway.cluster_sync import (
                notify as _notify, CHANNEL_QUOTA_BLOCKS,
            )
            await _notify(CHANNEL_QUOTA_BLOCKS, user_id)
        except Exception as exc:
            logger.debug("quota_reset_notify_failed: %s", exc)


# ── Process-wide instance ──────────────────────────────────────────


_engine: QuotaEngine | None = None


def init_engine(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    plan_registry: Any,
    flush_interval_seconds: int,
    redis_coordinator: Any | None = None,
) -> QuotaEngine:
    global _engine
    _engine = QuotaEngine(
        session_factory=session_factory,
        plan_registry=plan_registry,
        flush_interval_seconds=flush_interval_seconds,
        redis_coordinator=redis_coordinator,
    )
    return _engine


def get_engine() -> QuotaEngine:
    if _engine is None:
        raise RuntimeError("QuotaEngine not initialised - call init_engine() first")
    return _engine


# ── Helpers ────────────────────────────────────────────────────────


def _bucket_start_from_key(window_key: str) -> datetime | None:
    """Reverse `_bucket_key()` to recover the bucket start time."""
    parts = window_key.split("|", 2)
    if len(parts) != 3:
        return None
    try:
        return datetime.fromisoformat(parts[2])
    except Exception:
        return None


def _upsert_counter(row: dict[str, Any]):
    """UPSERT a counter row using Postgres' INSERT ... ON CONFLICT
    DO UPDATE. The gateway runs against Postgres only, so we don't
    bother with cross-dialect compatibility shims.
    """
    stmt = pg_insert(QuotaCounter).values(**row)
    return stmt.on_conflict_do_update(
        index_elements=["user_id", "metric", "window_key"],
        set_={"value": row["value"], "reset_at": row["reset_at"]},
    )


def _upsert_block(row: dict[str, Any]):
    stmt = pg_insert(QuotaBlock).values(**row)
    return stmt.on_conflict_do_update(
        index_elements=["user_id"],
        set_={
            "blocked_until": row["blocked_until"],
            "reason": row["reason"],
            "metric": row["metric"],
            "window": row["window"],
            "limit_value": row["limit_value"],
            "actual_value": row["actual_value"],
        },
    )
