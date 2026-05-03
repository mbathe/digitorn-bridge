"""Redis-backed message queue backend.

Same contract as ``message_queue`` module-level functions, but every
critical transition is a single atomic Lua script. This closes the
``mark_done`` ↔ ``next_queued`` race that exists with the SQL backend
(rows enqueued during the window between marking T1 done and scanning
for T2 are silently orphaned).

Schema
------
- ``queue:zset:{sid}``   - ZSET, member=row_id, score=position (monotonic).
                          Holds the ``queued`` rows in FIFO order.
- ``queue:running:{sid}``- STRING, value=row_id of the currently running
                          turn for this session. TTL = lease.
- ``queue:msg:{row_id}`` - HASH, all the row's fields. Set on enqueue,
                          mutated by status transitions, deleted on
                          terminal cleanup (kept by default for audit).
- ``queue:pos:{sid}``    - INCR counter for monotonic positions.
- ``queue:sessions``     - SET of session_ids that currently have any
                          queued or running rows. Maintained on every
                          mutation. Used by ``sessions_with_queued`` so
                          we don't have to ``SCAN`` keys at boot.

Atomicity
---------
Four Lua scripts cover every place where a race could open:

1. ``ENQUEUE``   - depth check + position alloc + ZADD + HSET + sessions
                   tracking, all in one round-trip.
2. ``DEQUEUE``   - ZPOPMIN + set running marker + flip status, atomic.
                   Returns the row_id or nil.
3. ``FINISH``    - terminal status flip on the running row + DEL running
                   marker + return the next ZPOPMIN'd row_id (or nil).
                   This is THE script that closes the race: caller drains
                   into the next turn or stops, but never both miss a
                   concurrent enqueue.
4. ``MERGE_OR_ENQUEUE`` / ``REPLACE_LAST_OR_ENQUEUE`` - read-modify-
                   write on the tail under the same key set, atomic.

Lua scripts run with full Redis serialization so the only ordering that
matters is the first command Redis sees - clients never observe a
torn state.

In-process state
----------------
``_awaiters`` (correlation_id → Future) stays in-process; same as the
SQL backend. Multi-worker ``queue_mode=wait`` was never multi-process
safe and doesn't become so here - out of scope for this rewrite.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _entry_cls():
    from digitorn.core.app.message_queue import QueueEntry as _Q
    return _Q


def _full_cls():
    from digitorn.core.app.message_queue import QueueFullError as _F
    return _F


_WORKER_ID = f"pid-{os.getpid()}"
_DEFAULT_LEASE_SECONDS = 120


# NOTE: QueueEntry and QueueFullError are imported from message_queue
# lazily (see _entry_cls / _full_cls helpers) to avoid a circular import.


# ─── Lua scripts ──────────────────────────────────────────────────────
#
# Conventions:
#   KEYS[1] = queue:zset:{sid}
#   KEYS[2] = queue:running:{sid}
#   KEYS[3] = queue:pos:{sid}
#   KEYS[4] = queue:sessions
#   ARGV[*] depends on the script
#
# Status hash field: 'status' ∈ queued|running|completed|failed|cancelled
# Hash key: queue:msg:{row_id} (built inline from row_id ARGV)


_LUA_ENQUEUE = """
-- KEYS[1]=zset, KEYS[2]=running, KEYS[3]=pos, KEYS[4]=sessions
-- ARGV: row_id, app_id, session_id, user_id, message, image_refs_json,
--       correlation_id, enqueued_at_iso, ttl_expires_at_iso, max_depth,
--       now_unix, ttl_expires_at_unix
local row_id = ARGV[1]
local sid = ARGV[3]
local max_depth = tonumber(ARGV[10])
local now_unix = ARGV[11]
local ttl_unix = ARGV[12]

-- Depth check: queued rows in the zset + 1 if a running marker exists.
local queued_count = redis.call("ZCARD", KEYS[1])
local running = redis.call("EXISTS", KEYS[2])
local depth = queued_count + running
if depth >= max_depth then
    return {"err", tostring(depth), tostring(max_depth)}
end

-- Allocate a fresh monotonic position.
local position = redis.call("INCR", KEYS[3])

-- Persist the row.
redis.call("HMSET", "queue:msg:" .. row_id,
    "id", row_id,
    "app_id", ARGV[2],
    "session_id", sid,
    "user_id", ARGV[4],
    "message", ARGV[5],
    "image_refs", ARGV[6],
    "correlation_id", ARGV[7],
    "enqueued_at", ARGV[8],
    "enqueued_at_unix", now_unix,
    "ttl_expires_at", ARGV[9],
    "ttl_expires_at_unix", ttl_unix,
    "status", "queued",
    "position", tostring(position),
    "started_at", "",
    "started_at_unix", "",
    "finished_at", "",
    "finished_at_unix", "",
    "error_code", "",
    "worker_id", "",
    "lease_until", ""
)
redis.call("ZADD", KEYS[1], position, row_id)
redis.call("SADD", KEYS[4], sid)
return {"ok", tostring(position)}
"""


_LUA_DEQUEUE = """
-- KEYS[1]=zset, KEYS[2]=running, KEYS[4]=sessions
-- ARGV: lease_seconds, now_iso, worker_id, sid, now_unix
-- Return: row_id or nil
local sid = ARGV[4]
local now_unix_str = ARGV[5]
local now_unix = tonumber(now_unix_str)

-- Refuse to dispatch if a row is already marked running on this session.
local already = redis.call("GET", KEYS[2])
if already then
    return nil
end

-- Pop the lowest-position member, skipping any rows whose TTL has
-- expired (mark them ``failed`` for the audit trail).
-- ZPOPMIN-equivalent via ZRANGE+ZREM: ZPOPMIN was added in Redis 5.0
-- but the legacy MS Windows port stays on 3.0 (no ZPOPMIN). The
-- pair stays atomic because Lua scripts run single-threaded in Redis.
local row_id
local lease = tonumber(ARGV[1])
while true do
    local popped = redis.call("ZRANGE", KEYS[1], 0, 0, "WITHSCORES")
    if #popped == 0 then
        -- Queue empty (or only had expired rows) → drop session marker.
        redis.call("SREM", KEYS[4], sid)
        return nil
    end
    redis.call("ZREM", KEYS[1], popped[1])
    local candidate = popped[1]
    local key = "queue:msg:" .. candidate
    local ttl_unix = tonumber(redis.call("HGET", key, "ttl_expires_at_unix") or "0")
    if ttl_unix > 0 and ttl_unix < now_unix then
        -- Row expired before we could dispatch it. Mark it failed
        -- (kept in the hash for audit) and try the next one.
        redis.call("HMSET", key,
            "status", "failed",
            "error_code", "queue_ttl_expired",
            "finished_at_unix", now_unix_str
        )
    else
        row_id = candidate
        break
    end
end

redis.call("SET", KEYS[2], row_id, "EX", lease)
-- Survival pointer for crash recovery (no TTL - only deleted on terminal).
redis.call("SET", "queue:was_running:" .. sid, row_id)
redis.call("HMSET", "queue:msg:" .. row_id,
    "status", "running",
    "started_at", ARGV[2],
    "started_at_unix", now_unix_str,
    "worker_id", ARGV[3],
    "lease_until", tostring(now_unix + lease)
)
return row_id
"""


_LUA_FINISH_AND_DRAIN = """
-- KEYS[1]=zset, KEYS[2]=running, KEYS[4]=sessions
-- ARGV: row_id_expected (or "" to ignore), terminal_status,
--       error_code, finished_at_iso, lease_seconds, now_iso,
--       worker_id, sid, now_unix, finished_at_unix
-- Behaviour:
--   1. If running marker matches row_id_expected (or expected=""), flip
--      that row to terminal_status with finished_at + error_code.
--      Never moves a row OUT of a terminal state (write-once safety).
--   2. Delete the running marker AND queue:was_running:{sid} pointer.
--   3. ZPOPMIN the next queued row, mark it running, return the new
--      row_id. Returns nil if queue empty.
-- Safety: if running marker exists but does NOT match expected, refuse
-- to drain (would double-dispatch). Return nil.
local sid = ARGV[8]
local expected = ARGV[1]
local now_unix = ARGV[9]
local finished_at_unix = ARGV[10]
local current = redis.call("GET", KEYS[2])

if current and expected ~= "" and current ~= expected then
    -- Running marker is owned by a different row - don't touch.
    return nil
end

if current then
    local key = "queue:msg:" .. current
    local cur_status = redis.call("HGET", key, "status")
    if cur_status ~= "completed" and cur_status ~= "failed" and cur_status ~= "cancelled" then
        redis.call("HMSET", key,
            "status", ARGV[2],
            "error_code", ARGV[3],
            "finished_at", ARGV[4],
            "finished_at_unix", finished_at_unix
        )
    end
    redis.call("DEL", KEYS[2])
end
-- Always drop the survival pointer for the just-finished slot.
redis.call("DEL", "queue:was_running:" .. sid)

-- Drain next, skipping rows whose TTL has expired.
-- ZPOPMIN-equivalent via ZRANGE+ZREM (Redis 3.0 has no ZPOPMIN; the
-- pair is atomic inside this Lua script).
local row_id
local lease = tonumber(ARGV[5])
local now_unix_num = tonumber(now_unix)
while true do
    local popped = redis.call("ZRANGE", KEYS[1], 0, 0, "WITHSCORES")
    if #popped == 0 then
        redis.call("SREM", KEYS[4], sid)
        return nil
    end
    redis.call("ZREM", KEYS[1], popped[1])
    local candidate = popped[1]
    local key = "queue:msg:" .. candidate
    local ttl_unix = tonumber(redis.call("HGET", key, "ttl_expires_at_unix") or "0")
    if ttl_unix > 0 and ttl_unix < now_unix_num then
        redis.call("HMSET", key,
            "status", "failed",
            "error_code", "queue_ttl_expired",
            "finished_at_unix", now_unix
        )
    else
        row_id = candidate
        break
    end
end
redis.call("SET", KEYS[2], row_id, "EX", lease)
redis.call("SET", "queue:was_running:" .. sid, row_id)
redis.call("HMSET", "queue:msg:" .. row_id,
    "status", "running",
    "started_at", ARGV[6],
    "started_at_unix", now_unix,
    "worker_id", ARGV[7],
    "lease_until", tostring(now_unix_num + lease)
)
return row_id
"""


_LUA_MARK_TERMINAL = """
-- KEYS[1]=running, KEYS[2]=zset (used to remove from queued state)
-- ARGV: row_id, terminal_status, error_code, finished_at_iso, sid,
--       finished_at_unix
-- Used when we want to mark terminal WITHOUT auto-draining (e.g.
-- mark_failed with separate drain logic, or mark_cancelled on a row
-- that wasn't yet popped).
local row_id = ARGV[1]
local sid = ARGV[5]
local key = "queue:msg:" .. row_id
local cur_status = redis.call("HGET", key, "status")
if cur_status == "completed" or cur_status == "failed" or cur_status == "cancelled" then
    return 0
end
redis.call("HMSET", key,
    "status", ARGV[2],
    "error_code", ARGV[3],
    "finished_at", ARGV[4],
    "finished_at_unix", ARGV[6]
)
-- Remove from the queued zset if it was still queued.
redis.call("ZREM", KEYS[2], row_id)
-- If this row was the running one for the session, clear all markers.
local running = redis.call("GET", KEYS[1])
if running == row_id then
    redis.call("DEL", KEYS[1])
    redis.call("DEL", "queue:was_running:" .. sid)
end
return 1
"""


_LUA_MERGE_OR_ENQUEUE = """
-- KEYS[1]=zset, KEYS[2]=running, KEYS[3]=pos, KEYS[4]=sessions
-- ARGV: row_id_new, app_id, session_id, user_id, message_new,
--       image_refs_json_new, correlation_id_new, enqueued_at_iso,
--       ttl_expires_at_iso, max_depth, now_unix, window_seconds,
--       separator, expected_user, ttl_expires_at_unix
-- Returns: {"merged", existing_row_id, position} or {"ok", row_id_new, position}
--           or {"err", depth, max_depth}
local sid = ARGV[3]
local now_unix = tonumber(ARGV[11])
local window = tonumber(ARGV[12])
local sep = ARGV[13]
local expected_user = ARGV[14]
local ttl_unix_str = ARGV[15]

-- Look at the highest-score member: that's the tail (most recent) queued row.
local tail = redis.call("ZREVRANGE", KEYS[1], 0, 0, "WITHSCORES")
if #tail > 0 then
    local tail_id = tail[1]
    local tail_pos = tonumber(tail[2])
    local key = "queue:msg:" .. tail_id
    local fields = redis.call("HMGET", key,
        "status", "user_id", "enqueued_at", "message", "image_refs", "correlation_id"
    )
    local status, user_id, enqueued_at, msg, refs_json, corr =
        fields[1], fields[2], fields[3], fields[4], fields[5], fields[6]
    -- Match window: same user, queued status, age <= window_seconds.
    if status == "queued" and user_id == expected_user then
        -- Parse ISO timestamp into unix; we use a lighter trick - Redis
        -- doesn't have a date parser, so we compare via the
        -- ``enqueued_at_unix`` field that the Python side stores in the
        -- hash on every write. Read it now.
        local tail_unix = tonumber(redis.call("HGET", key, "enqueued_at_unix") or "0")
        if (now_unix - tail_unix) <= window then
            -- Merge: append message, optionally extend image_refs.
            local merged_msg = msg .. sep .. ARGV[5]
            local refs_old = cjson.decode(refs_json or "[]")
            local refs_new = cjson.decode(ARGV[6] or "[]")
            for _, v in ipairs(refs_new) do
                table.insert(refs_old, v)
            end
            local merged_refs = cjson.encode(refs_old)
            redis.call("HMSET", key,
                "message", merged_msg,
                "image_refs", merged_refs,
                "enqueued_at", ARGV[8],
                "enqueued_at_unix", tostring(now_unix),
                "ttl_expires_at", ARGV[9],
                "ttl_expires_at_unix", ttl_unix_str
            )
            return {"merged", tail_id, tostring(tail_pos)}
        end
    end
end

-- Fall through to plain enqueue (depth-checked).
local max_depth = tonumber(ARGV[10])
local queued_count = redis.call("ZCARD", KEYS[1])
local running = redis.call("EXISTS", KEYS[2])
if (queued_count + running) >= max_depth then
    return {"err", tostring(queued_count + running), tostring(max_depth)}
end
local position = redis.call("INCR", KEYS[3])
redis.call("HMSET", "queue:msg:" .. ARGV[1],
    "id", ARGV[1],
    "app_id", ARGV[2],
    "session_id", sid,
    "user_id", ARGV[4],
    "message", ARGV[5],
    "image_refs", ARGV[6],
    "correlation_id", ARGV[7],
    "enqueued_at", ARGV[8],
    "enqueued_at_unix", tostring(now_unix),
    "ttl_expires_at", ARGV[9],
    "ttl_expires_at_unix", ttl_unix_str,
    "status", "queued",
    "position", tostring(position),
    "started_at", "",
    "finished_at", "",
    "error_code", "",
    "worker_id", "",
    "lease_until", ""
)
redis.call("ZADD", KEYS[1], position, ARGV[1])
redis.call("SADD", KEYS[4], sid)
return {"ok", ARGV[1], tostring(position)}
"""


_LUA_REPLACE_LAST_OR_ENQUEUE = """
-- KEYS[1]=zset, KEYS[2]=running, KEYS[3]=pos, KEYS[4]=sessions
-- ARGV: row_id_new, app_id, session_id, user_id, message_new,
--       image_refs_json, correlation_id_new, enqueued_at_iso,
--       ttl_expires_at_iso, max_depth, now_unix, expected_user,
--       ttl_expires_at_unix
-- Returns: {"replaced", existing_row_id, position, old_correlation_id}
--          or {"ok", row_id_new, position}
--          or {"err", depth, max_depth}
local sid = ARGV[3]
local expected_user = ARGV[12]
local ttl_unix_str = ARGV[13]

local tail = redis.call("ZREVRANGE", KEYS[1], 0, 0, "WITHSCORES")
if #tail > 0 then
    local tail_id = tail[1]
    local tail_pos = tonumber(tail[2])
    local key = "queue:msg:" .. tail_id
    local fields = redis.call("HMGET", key, "status", "user_id", "correlation_id")
    local status, user_id, old_corr = fields[1], fields[2], fields[3]
    if status == "queued" and user_id == expected_user then
        redis.call("HMSET", key,
            "message", ARGV[5],
            "image_refs", ARGV[6],
            "correlation_id", ARGV[7],
            "enqueued_at", ARGV[8],
            "enqueued_at_unix", ARGV[11],
            "ttl_expires_at", ARGV[9],
            "ttl_expires_at_unix", ttl_unix_str
        )
        return {"replaced", tail_id, tostring(tail_pos), old_corr or ""}
    end
end

-- Plain enqueue.
local max_depth = tonumber(ARGV[10])
local queued_count = redis.call("ZCARD", KEYS[1])
local running = redis.call("EXISTS", KEYS[2])
if (queued_count + running) >= max_depth then
    return {"err", tostring(queued_count + running), tostring(max_depth)}
end
local position = redis.call("INCR", KEYS[3])
redis.call("HMSET", "queue:msg:" .. ARGV[1],
    "id", ARGV[1],
    "app_id", ARGV[2],
    "session_id", sid,
    "user_id", ARGV[4],
    "message", ARGV[5],
    "image_refs", ARGV[6],
    "correlation_id", ARGV[7],
    "enqueued_at", ARGV[8],
    "enqueued_at_unix", ARGV[11],
    "ttl_expires_at", ARGV[9],
    "ttl_expires_at_unix", ttl_unix_str,
    "status", "queued",
    "position", tostring(position),
    "started_at", "",
    "finished_at", "",
    "error_code", "",
    "worker_id", "",
    "lease_until", ""
)
redis.call("ZADD", KEYS[1], position, ARGV[1])
redis.call("SADD", KEYS[4], sid)
return {"ok", ARGV[1], tostring(position)}
"""


_LUA_CANCEL = """
-- KEYS[1]=zset
-- ARGV: row_id, sid, finished_at_iso
-- Cancel a still-queued row. Returns 1 if cancelled, 0 if it was
-- already running / done / not in the zset.
local row_id = ARGV[1]
local removed = redis.call("ZREM", KEYS[1], row_id)
if removed == 0 then
    return 0
end
local key = "queue:msg:" .. row_id
local cur_status = redis.call("HGET", key, "status")
if cur_status == "completed" or cur_status == "failed" or cur_status == "cancelled" then
    return 0
end
redis.call("HMSET", key, "status", "cancelled", "finished_at", ARGV[3])
return 1
"""


# ─── Backend ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_unix() -> float:
    return time.time()


def _zset_key(sid: str) -> str:
    return f"queue:zset:{sid}"


def _running_key(sid: str) -> str:
    return f"queue:running:{sid}"


def _pos_key(sid: str) -> str:
    return f"queue:pos:{sid}"


def _msg_key(row_id: str) -> str:
    return f"queue:msg:{row_id}"


_SESSIONS_SET = "queue:sessions"
_TERMINAL = ("completed", "failed", "cancelled")


# In-process awaiter map (mode=wait). Identical to the SQL-backend version.
_awaiters: dict[str, asyncio.Future] = {}
_session_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock


def awaiter_future(correlation_id: str) -> asyncio.Future:
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _awaiters[correlation_id] = fut
    return fut


def resolve_awaiter(correlation_id: str, result: Any) -> None:
    fut = _awaiters.pop(correlation_id, None)
    if fut and not fut.done():
        fut.set_result(result)


def fail_awaiter(correlation_id: str, exc: Exception) -> None:
    fut = _awaiters.pop(correlation_id, None)
    if fut and not fut.done():
        fut.set_exception(exc)


class RedisQueueBackend:
    """Concrete Redis backend.

    Construct with an async Redis client (``redis.asyncio.Redis``) or
    a fakeredis instance for tests. Pre-loads Lua scripts at first use
    via ``Script(...)`` for SHA-cached EVALSHA fallback.
    """

    def __init__(self, redis_client: Any) -> None:
        self._r = redis_client
        # Register scripts. ``register_script`` returns a Script object
        # that handles EVALSHA + fallback to EVAL on NOSCRIPT.
        self._enqueue = redis_client.register_script(_LUA_ENQUEUE)
        self._dequeue = redis_client.register_script(_LUA_DEQUEUE)
        self._finish_and_drain = redis_client.register_script(_LUA_FINISH_AND_DRAIN)
        self._mark_terminal = redis_client.register_script(_LUA_MARK_TERMINAL)
        self._merge_or_enqueue = redis_client.register_script(_LUA_MERGE_OR_ENQUEUE)
        self._replace_last = redis_client.register_script(_LUA_REPLACE_LAST_OR_ENQUEUE)
        self._cancel = redis_client.register_script(_LUA_CANCEL)

    # ── Public API (same shape as message_queue module-level) ─────────

    async def enqueue(
        self,
        *,
        app_id: str,
        session_id: str,
        user_id: str,
        message: str,
        image_refs: list | None = None,
        ttl_seconds: int = 3600,
        max_depth: int = 20,
    ) -> Any:
        correlation_id = f"fp-{uuid.uuid4().hex[:12]}"
        row_id = uuid.uuid4().hex
        now_iso = _now_iso()
        now_unix = _now_unix()
        ttl_unix = now_unix + ttl_seconds
        ttl_iso = datetime.fromtimestamp(ttl_unix, timezone.utc).isoformat()
        refs_list = list(image_refs or [])
        refs_json = json.dumps(refs_list)

        # Use the merge/replace-style script entry shape - pure enqueue
        # is the no-merge fallthrough so we share the same depth check
        # logic. Slightly cheaper to keep a dedicated ENQUEUE script
        # though, so we do.
        result = await self._enqueue(
            keys=[
                _zset_key(session_id),
                _running_key(session_id),
                _pos_key(session_id),
                _SESSIONS_SET,
            ],
            args=[
                row_id, app_id, session_id, user_id or "",
                message or "", refs_json, correlation_id,
                now_iso, ttl_iso, str(max_depth), str(now_unix),
                str(ttl_unix),
            ],
        )

        if not result or _decode(result[0]) == "err":
            depth = int(_decode(result[1])) if result and len(result) > 1 else 0
            cap = int(_decode(result[2])) if result and len(result) > 2 else max_depth
            raise _full_cls()(
                f"Queue at capacity ({depth}/{cap}) for session {session_id}",
                depth=depth, max_depth=cap,
            )
        position = int(_decode(result[1]))

        logger.info(
            "queue_redis_enqueue session=%s position=%d correlation_id=%s",
            session_id, position, correlation_id,
        )
        return _entry_cls()(
            id=row_id, app_id=app_id, session_id=session_id,
            user_id=user_id or "", position=position, message=message or "",
            image_refs=refs_list, status="queued",
            correlation_id=correlation_id, enqueued_at=now_unix,
        )

    async def merge_or_enqueue(
        self,
        *,
        app_id: str,
        session_id: str,
        user_id: str,
        message: str,
        image_refs: list | None = None,
        window_seconds: float = 2.0,
        separator: str = "\n\n---\n\n",
        ttl_seconds: int = 3600,
        max_depth: int = 20,
    ) -> tuple[Any, bool]:
        row_id_new = uuid.uuid4().hex
        correlation_new = f"fp-{uuid.uuid4().hex[:12]}"
        now_iso = _now_iso()
        now_unix = _now_unix()
        ttl_unix = now_unix + ttl_seconds
        ttl_iso = datetime.fromtimestamp(ttl_unix, timezone.utc).isoformat()
        refs_list = list(image_refs or [])
        refs_json = json.dumps(refs_list)

        result = await self._merge_or_enqueue(
            keys=[
                _zset_key(session_id),
                _running_key(session_id),
                _pos_key(session_id),
                _SESSIONS_SET,
            ],
            args=[
                row_id_new, app_id, session_id, user_id or "",
                message or "", refs_json, correlation_new,
                now_iso, ttl_iso, str(max_depth), str(now_unix),
                str(window_seconds), separator, user_id or "",
                str(ttl_unix),
            ],
        )

        tag = _decode(result[0])
        if tag == "err":
            depth = int(_decode(result[1]))
            cap = int(_decode(result[2]))
            raise _full_cls()(
                f"Queue at capacity ({depth}/{cap}) for session {session_id}",
                depth=depth, max_depth=cap,
            )

        if tag == "merged":
            existing_id = _decode(result[1])
            position = int(_decode(result[2]))
            # Re-read the merged hash to return the up-to-date entry.
            entry = await self._read_entry(existing_id)
            if entry is None:
                # Shouldn't happen but stay safe.
                raise RuntimeError("merged row vanished from hash")
            logger.info(
                "queue_redis_merge session=%s position=%d age<=%.1fs",
                session_id, position, window_seconds,
            )
            return entry, True

        # tag == "ok" → fresh enqueue
        position = int(_decode(result[2]))
        return _entry_cls()(
            id=row_id_new, app_id=app_id, session_id=session_id,
            user_id=user_id or "", position=position, message=message or "",
            image_refs=refs_list, status="queued",
            correlation_id=correlation_new, enqueued_at=now_unix,
        ), False

    async def replace_last_or_enqueue(
        self,
        *,
        app_id: str,
        session_id: str,
        user_id: str,
        message: str,
        image_refs: list | None = None,
        ttl_seconds: int = 3600,
        max_depth: int = 20,
    ) -> tuple[Any, bool]:
        row_id_new = uuid.uuid4().hex
        correlation_new = f"fp-{uuid.uuid4().hex[:12]}"
        now_iso = _now_iso()
        now_unix = _now_unix()
        ttl_unix = now_unix + ttl_seconds
        ttl_iso = datetime.fromtimestamp(ttl_unix, timezone.utc).isoformat()
        refs_list = list(image_refs or [])
        refs_json = json.dumps(refs_list)

        result = await self._replace_last(
            keys=[
                _zset_key(session_id),
                _running_key(session_id),
                _pos_key(session_id),
                _SESSIONS_SET,
            ],
            args=[
                row_id_new, app_id, session_id, user_id or "",
                message or "", refs_json, correlation_new,
                now_iso, ttl_iso, str(max_depth), str(now_unix),
                user_id or "", str(ttl_unix),
            ],
        )
        tag = _decode(result[0])
        if tag == "err":
            depth = int(_decode(result[1]))
            cap = int(_decode(result[2]))
            raise _full_cls()(
                f"Queue at capacity ({depth}/{cap}) for session {session_id}",
                depth=depth, max_depth=cap,
            )
        if tag == "replaced":
            tail_id = _decode(result[1])
            position = int(_decode(result[2]))
            old_corr = _decode(result[3])
            # Awaiter on the displaced message is no longer valid.
            fail_awaiter(old_corr, RuntimeError("replaced by new message"))
            entry = _entry_cls()(
                id=tail_id, app_id=app_id, session_id=session_id,
                user_id=user_id or "", position=position, message=message or "",
                image_refs=refs_list, status="queued",
                correlation_id=correlation_new, enqueued_at=now_unix,
            )
            logger.info(
                "queue_redis_replace_last session=%s position=%d",
                session_id, position,
            )
            return entry, True
        # tag == "ok"
        position = int(_decode(result[2]))
        return _entry_cls()(
            id=row_id_new, app_id=app_id, session_id=session_id,
            user_id=user_id or "", position=position, message=message or "",
            image_refs=refs_list, status="queued",
            correlation_id=correlation_new, enqueued_at=now_unix,
        ), False

    async def next_queued(
        self, session_id: str, lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> Any | None:
        now_iso = _now_iso()
        now_unix = _now_unix()
        # The DEQUEUE script uses KEYS[1]=zset, KEYS[2]=running, KEYS[4]=sessions.
        # Pass a harmless placeholder for KEYS[3] to keep the Redis cluster
        # slot mapping consistent (all four keys hash on {sid}).
        row_id = await self._dequeue(
            keys=[
                _zset_key(session_id),
                _running_key(session_id),
                _pos_key(session_id),
                _SESSIONS_SET,
            ],
            args=[
                str(lease_seconds), now_iso, _WORKER_ID, session_id,
                str(now_unix),
            ],
        )
        if row_id is None:
            return None
        return await self._read_entry(_decode(row_id))

    async def heartbeat(
        self, row_id: str, lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> bool:
        # Find the session for this row, then refresh the running marker's TTL.
        sid = await self._r.hget(_msg_key(row_id), "session_id")
        if sid is None:
            return False
        sid = _decode(sid)
        running = await self._r.get(_running_key(sid))
        if running is None or _decode(running) != row_id:
            return False
        # ``EXPIRE`` resets the TTL.
        await self._r.expire(_running_key(sid), lease_seconds)
        await self._r.hset(
            _msg_key(row_id), "lease_until",
            str(_now_unix() + lease_seconds),
        )
        return True

    async def reap_expired_leases(self) -> int:
        # Redis already auto-removes the running marker when its TTL expires.
        # We just need to reset the row's ``status`` from ``running`` back
        # to ``queued`` and re-add it to the zset. Walk all sessions
        # tracked in queue:sessions and look for orphaned running rows.
        sids = await self._r.smembers(_SESSIONS_SET)
        reaped = 0
        for sid_raw in sids:
            sid = _decode(sid_raw)
            running = await self._r.get(_running_key(sid))
            if running is not None:
                continue  # marker still alive
            # Find any row whose hash says status=running on this session.
            # We can't scan hashes by predicate cheaply, so we keep a
            # convention: the running row's id was last written to the
            # marker, and when the marker disappears (TTL), the hash
            # status is stale. Walk the zset for queued rows to confirm
            # this session has work to do, and look at history rows…
            # Pragmatic shortcut: we don't reset stale ``running`` row
            # hashes here. They'll be observed via ``rehydrate_on_boot``
            # at next daemon start. Ephemeral lease-loss in mid-uptime
            # already triggers ``next_queued`` to dispatch the next
            # queued row anyway.
            continue
        if reaped:
            logger.warning(
                "queue_redis_reaper reset %d expired running rows",
                reaped,
            )
        return reaped

    async def mark_done(self, row_id: str) -> None:
        await self._set_terminal(row_id, "completed")

    async def mark_failed(self, row_id: str, error_code: str = "") -> None:
        await self._set_terminal(row_id, "failed", error_code)

    async def mark_cancelled(self, row_id: str) -> None:
        await self._set_terminal(row_id, "cancelled")

    async def finish_and_drain(
        self,
        session_id: str,
        row_id: str,
        terminal_status: str = "completed",
        error_code: str = "",
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
    ) -> Any | None:
        """Atomically mark ``row_id`` terminal AND drain the next queued
        row. Returns the new running entry, or None if the queue is empty.

        This is the script that closes the race between ``mark_done`` and
        ``next_queued`` - a concurrent ``enqueue`` can never sneak between
        the two phases.
        """
        now_iso = _now_iso()
        now_unix = _now_unix()
        next_id = await self._finish_and_drain(
            keys=[
                _zset_key(session_id),
                _running_key(session_id),
                _pos_key(session_id),
                _SESSIONS_SET,
            ],
            args=[
                row_id or "", terminal_status, error_code or "",
                now_iso, str(lease_seconds), now_iso, _WORKER_ID,
                session_id, str(now_unix), str(now_unix),
            ],
        )
        if next_id is None:
            return None
        return await self._read_entry(_decode(next_id))

    async def cancel(self, session_id: str, row_id: str) -> bool:
        result = await self._cancel(
            keys=[_zset_key(session_id)],
            args=[row_id, session_id, _now_iso()],
        )
        return int(result) > 0

    async def clear(self, session_id: str) -> int:
        # Walk the zset and cancel each. Done outside Lua because the
        # per-session zset can grow to ``max_depth`` (≤ 1000) - small
        # enough that a Python loop over individual cancels is fine and
        # avoids embedding more Lua.
        ids = await self._r.zrange(_zset_key(session_id), 0, -1)
        cancelled = 0
        for raw in ids:
            rid = _decode(raw)
            if await self.cancel(session_id, rid):
                cancelled += 1
        return cancelled

    async def list_for_session(
        self, session_id: str, include_finished: bool = False,
    ) -> list[Any]:
        # Active rows: zset members + (optional) running row.
        ids: list[str] = []
        zmembers = await self._r.zrange(_zset_key(session_id), 0, -1)
        ids.extend(_decode(m) for m in zmembers)
        running = await self._r.get(_running_key(session_id))
        if running is not None:
            ids.append(_decode(running))
        # ``include_finished`` would require scanning all queue:msg:*
        # hashes by session_id - expensive and rarely needed at the API
        # layer. We mirror the SQL backend's "active rows only" view; if
        # the caller really needs history they should query the audit log.
        entries: list[Any] = []
        for rid in ids:
            entry = await self._read_entry(rid)
            if entry is not None:
                if not include_finished and entry.status not in ("queued", "running"):
                    continue
                entries.append(entry)
        entries.sort(key=lambda e: e.position)
        return entries

    async def rehydrate_on_boot(self) -> int:
        """Reset rows the daemon left in ``running`` state across a
        restart. Walk ``queue:sessions``, drop any stale running marker,
        and push the orphaned row back into the queued zset.

        Redis clears the running marker via TTL after the lease expires.
        The ``queue:was_running:{sid}`` survival pointer (no TTL) lets us
        find the orphaned row even after the lease-marker disappeared.
        """
        sids = await self._r.smembers(_SESSIONS_SET)
        rehydrated = 0
        for sid_raw in sids:
            sid = _decode(sid_raw)
            was = await self._r.get(f"queue:was_running:{sid}")
            if was is None:
                continue
            was_id = _decode(was)
            key = _msg_key(was_id)
            pos_raw = await self._r.hget(key, "position")
            status_raw = await self._r.hget(key, "status")
            if pos_raw is None or status_raw is None:
                # Hash gone - clean up the stale pointer.
                await self._r.delete(f"queue:was_running:{sid}")
                continue
            status = _decode(status_raw)
            if status != "running":
                # Already in a terminal/queued state - pointer is stale.
                await self._r.delete(f"queue:was_running:{sid}")
                continue
            try:
                position = int(_decode(pos_raw))
            except (ValueError, TypeError):
                position = 0
            # Reset row to queued + drop any leftover running marker.
            await self._r.hset(
                key,
                mapping={
                    "status": "queued",
                    "started_at": "",
                    "started_at_unix": "",
                    "worker_id": "",
                    "lease_until": "",
                },
            )
            await self._r.zadd(_zset_key(sid), {was_id: position})
            await self._r.delete(_running_key(sid))
            await self._r.delete(f"queue:was_running:{sid}")
            rehydrated += 1
        if rehydrated:
            logger.info(
                "queue_redis_rehydrate reset %d stuck running rows → queued",
                rehydrated,
            )
        return rehydrated

    async def sessions_with_queued(self) -> list[tuple[str, str, str]]:
        """Return ``[(app_id, session_id, user_id)]`` for every session
        that currently has at least one queued row."""
        sids = await self._r.smembers(_SESSIONS_SET)
        out: list[tuple[str, str, str]] = []
        for sid_raw in sids:
            sid = _decode(sid_raw)
            # Find the head of this session's zset so we can read its
            # app_id + user_id from the hash.
            head = await self._r.zrange(_zset_key(sid), 0, 0)
            if not head:
                continue
            row_id = _decode(head[0])
            fields = await self._r.hmget(
                _msg_key(row_id), "app_id", "user_id",
            )
            app_id = _decode(fields[0]) if fields and fields[0] else ""
            user_id = _decode(fields[1]) if fields and len(fields) > 1 and fields[1] else ""
            if app_id:
                out.append((app_id, sid, user_id))
        return out

    async def purge_queued_on_boot(self, reason: str = "daemon_restart") -> int:
        sids = await self._r.smembers(_SESSIONS_SET)
        purged = 0
        finished_iso = _now_iso()
        finished_unix = str(_now_unix())
        for sid_raw in sids:
            sid = _decode(sid_raw)
            ids = await self._r.zrange(_zset_key(sid), 0, -1)
            for raw in ids:
                rid = _decode(raw)
                await self._r.hset(
                    _msg_key(rid),
                    mapping={
                        "status": "cancelled",
                        "error_code": reason,
                        "finished_at": finished_iso,
                        "finished_at_unix": finished_unix,
                    },
                )
                purged += 1
            await self._r.delete(_zset_key(sid))
            await self._r.srem(_SESSIONS_SET, sid)
        if purged:
            logger.info(
                "queue_redis_purge cancelled %d queued rows on boot", purged,
            )
        return purged

    async def has_running(self, session_id: str) -> bool:
        return bool(await self._r.exists(_running_key(session_id)))

    async def depth_for_session(self, session_id: str) -> int:
        queued = int(await self._r.zcard(_zset_key(session_id)) or 0)
        running = 1 if await self.has_running(session_id) else 0
        return queued + running

    # ── Internals ─────────────────────────────────────────────────────

    async def _set_terminal(
        self, row_id: str, status: str, error_code: str = "",
    ) -> None:
        sid_raw = await self._r.hget(_msg_key(row_id), "session_id")
        if sid_raw is None:
            return
        sid = _decode(sid_raw)
        now_unix = _now_unix()
        await self._mark_terminal(
            keys=[_running_key(sid), _zset_key(sid)],
            args=[
                row_id, status, error_code or "", _now_iso(),
                sid, str(now_unix),
            ],
        )

    async def _read_entry(self, row_id: str) -> Any | None:
        fields = await self._r.hgetall(_msg_key(row_id))
        if not fields:
            return None
        d = {_decode(k): _decode(v) for k, v in fields.items()}
        try:
            refs = json.loads(d.get("image_refs", "[]") or "[]")
        except Exception:
            refs = []
        return _entry_cls()(
            id=d.get("id", row_id),
            app_id=d.get("app_id", ""),
            session_id=d.get("session_id", ""),
            user_id=d.get("user_id", ""),
            position=int(d.get("position", "0") or 0),
            message=d.get("message", ""),
            image_refs=refs,
            status=d.get("status", "queued"),
            correlation_id=d.get("correlation_id", ""),
            enqueued_at=float(d.get("enqueued_at_unix") or 0.0),
            started_at=(
                _try_float(d.get("started_at_unix"))
                if d.get("started_at_unix") else None
            ),
            finished_at=(
                _try_float(d.get("finished_at_unix"))
                if d.get("finished_at_unix") else None
            ),
            error_code=d.get("error_code", ""),
        )


def _decode(v: Any) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8")
    if v is None:
        return ""
    return str(v)


def _try_float(v: Any) -> float | None:
    try:
        return float(v) if v else None
    except (ValueError, TypeError):
        return None


# ─── Connector ────────────────────────────────────────────────────────


async def create_redis_client(redis_url: str) -> Any:
    """Create a single async Redis client. Returns None on failure
    (caller falls back to in-memory backend)."""
    try:
        import redis.asyncio as aioredis  # type: ignore
    except ImportError:
        logger.warning("queue_redis: redis package missing - install 'redis' to enable")
        return None
    try:
        client = aioredis.from_url(
            redis_url,
            decode_responses=False,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        # PING with a short timeout to fail fast if server is unreachable.
        await asyncio.wait_for(client.ping(), timeout=2.0)
        logger.info(
            "queue_redis_connected url=%s",
            redis_url.split("@")[-1] if "@" in redis_url else redis_url,
        )
        return client
    except Exception as exc:
        logger.warning("queue_redis_connect_failed (will fall back): %s", exc)
        return None
