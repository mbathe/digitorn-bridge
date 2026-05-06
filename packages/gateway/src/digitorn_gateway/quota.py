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

        # Background task handle; None until start().
        self._flush_task: asyncio.Task | None = None
        self._stopped = False

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
        """O(M). Increment every counter touched by this call, then
        check whether any rule was crossed. Sets a sticky block on the
        first overflow encountered.

        Awaited but does no I/O - safe to call inline from a request
        handler. The actual DB write is amortised by the background
        flush task.
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

        first_overflow: tuple[str, str, float, float] | None = None  # (metric, window, limit, actual)

        # When Redis coordination is alive, ``record()`` walks the rules
        # to identify the (metric, window, bucket) tuples + atomically
        # increments them in Redis, comparing the cluster-wide value to
        # the limit. Otherwise we use the legacy local counter (still
        # accurate for single-worker deploys).
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
                if first_overflow is None and bucket["value"] > rule.limit:
                    first_overflow = (
                        metric, window_name, rule.limit, bucket["value"],
                    )
                # Mark dirty for the flush. Single pair per touched bucket.
                self._dirty.add((record.user_id, bucket["key"]))
                # Stash for cross-worker reconciliation. We do the actual
                # Redis call AFTER the local pass so a Redis hiccup never
                # delays the local snapshot the next request can read.
                if redis_alive:
                    redis_targets.append(
                        (metric, window_name, delta, start, end, rule.limit),
                    )

        # Cross-worker reconciliation. Fire-and-forget: the request that
        # triggered ``record()`` already received its response (this
        # method is called from a FastAPI BackgroundTask). The Redis
        # round-trips happen on the background task's own time and never
        # touch the request hot path.
        #
        # We retain a strong reference in ``self._reconcile_tasks`` so
        # asyncio's task GC doesn't reap the task before it completes
        # (3.10+ stores a weakref by default).
        if redis_alive and redis_targets:
            task = asyncio.create_task(
                self._redis_reconcile(
                    record.user_id, plan_def, redis_targets,
                ),
                name="quota-redis-reconcile",
            )
            self._reconcile_tasks.add(task)
            task.add_done_callback(self._reconcile_tasks.discard)

        if first_overflow is not None:
            metric, window, limit_v, actual_v = first_overflow
            await self._set_block(
                user_id=record.user_id,
                metric=metric,
                window=window,
                limit_value=limit_v,
                actual_value=actual_v,
                blocked_until=self._block_expiry_for(window, plan_def, metric),
            )

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

    def start(self) -> None:
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(
                self._flush_loop(), name="quota-flush-loop",
            )

    async def stop(self) -> None:
        self._stopped = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
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

    # ── Admin helpers ──────────────────────────────────────────

    async def reset_user(self, user_id: str) -> None:
        """Wipe a user's in-memory + persisted counters + block. Used
        by the admin "reset quota" route.
        """
        self._counters.pop(user_id, None)
        self._blocks.pop(user_id, None)
        # Drop any dirty markers tied to this user.
        self._dirty = {(u, k) for (u, k) in self._dirty if u != user_id}
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
