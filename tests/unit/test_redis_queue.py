"""Unit tests for the Redis-backed message queue.

Uses ``fakeredis.aioredis`` so the suite runs in-process - no real
Redis daemon needed. The fakeredis async client implements the same
``register_script`` / EVALSHA contract we rely on.

What we cover:

- Round-trip: enqueue → next_queued → finish_and_drain (race fix).
- FIFO ordering across many enqueues.
- Concurrent enqueue while ``finish_and_drain`` is in flight (the race
  the SQL backend has - Redis backend MUST drain it).
- ``merge_or_enqueue`` window check: merges within window, falls
  through outside.
- ``replace_last_or_enqueue`` overwrites the tail and busts the old
  awaiter.
- ``cancel`` removes a queued row; cannot cancel a running row.
- ``has_running`` / ``depth_for_session`` reflect current state.
- ``rehydrate_on_boot`` recovers a row left in ``running`` after a
  pretend-crash (manually corrupting Redis to mimic the daemon dying).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from digitorn.core.app.message_queue import QueueFullError
from digitorn.core.app.queue_redis import RedisQueueBackend


@pytest.fixture
async def redis_client():
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=False)
    yield client
    try:
        await client.flushall()
    except Exception:
        pass
    try:
        await client.aclose()
    except Exception:
        pass


@pytest.fixture
async def backend(redis_client):
    return RedisQueueBackend(redis_client)


# ─── Basic round-trip ─────────────────────────────────────────────────


async def test_enqueue_then_next_queued(backend):
    e1 = await backend.enqueue(
        app_id="app1", session_id="s1", user_id="u1",
        message="hello",
    )
    assert e1.status == "queued"
    assert e1.position == 1
    assert e1.correlation_id.startswith("fp-")

    head = await backend.next_queued("s1")
    assert head is not None
    assert head.id == e1.id
    assert head.status == "running"


async def test_next_queued_refuses_when_already_running(backend):
    await backend.enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="first",
    )
    first = await backend.next_queued("s1")
    assert first is not None

    # Second enqueue while a turn is running - goes into the zset.
    await backend.enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="second",
    )

    # next_queued refuses because the running marker exists.
    second = await backend.next_queued("s1")
    assert second is None


# ─── The race fix: finish_and_drain is atomic ─────────────────────────


async def test_finish_and_drain_picks_up_concurrent_enqueue(backend):
    """The bug we set out to fix: a turn finishes, marks T1 done, and
    a new T2 message arrives. With Redis Lua atomicity, T2 cannot fall
    through the cracks - finish_and_drain returns it (or a subsequent
    next_queued does)."""
    e1 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u", message="one",
    )
    started = await backend.next_queued("s1")
    assert started.id == e1.id

    # Concurrently enqueue T2 and finish T1.
    async def enqueue_t2():
        await asyncio.sleep(0)
        return await backend.enqueue(
            app_id="a", session_id="s1", user_id="u", message="two",
        )

    async def finish_t1():
        return await backend.finish_and_drain("s1", e1.id, "completed")

    e2_task = asyncio.create_task(enqueue_t2())
    drained = await finish_t1()
    e2 = await e2_task

    # Either:
    #   (a) drained returned T2 directly (concurrent enqueue landed
    #       before finish_and_drain's ZPOPMIN), OR
    #   (b) drained returned None and a follow-up next_queued() picks
    #       up T2.
    if drained is None:
        # T2 landed AFTER the script's pop. Verify it's still queued
        # and dispatchable.
        assert await backend.has_running("s1") is False
        followup = await backend.next_queued("s1")
        assert followup is not None
        assert followup.id == e2.id
    else:
        assert drained.id == e2.id


async def test_finish_and_drain_returns_none_when_queue_empty(backend):
    e1 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u", message="solo",
    )
    started = await backend.next_queued("s1")
    assert started.id == e1.id

    drained = await backend.finish_and_drain("s1", e1.id, "completed")
    assert drained is None
    assert await backend.has_running("s1") is False


async def test_finish_and_drain_refuses_when_marker_mismatch(backend):
    """Safety: if the running marker points at a different row (which
    shouldn't happen, but defense in depth), finish_and_drain MUST NOT
    drain - it would double-dispatch."""
    e1 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u", message="one",
    )
    await backend.next_queued("s1")  # marks e1 running

    # Try to finish a different (non-existent) row.
    drained = await backend.finish_and_drain(
        "s1", "garbage-row-id", "completed",
    )
    assert drained is None
    # e1 is still running.
    assert await backend.has_running("s1")


# ─── FIFO ordering ────────────────────────────────────────────────────


async def test_fifo_across_many_enqueues(backend):
    enqueued = []
    for i in range(10):
        e = await backend.enqueue(
            app_id="a", session_id="s1", user_id="u",
            message=f"msg-{i}",
        )
        enqueued.append(e)

    # Drain in order.
    drained = []
    head = await backend.next_queued("s1")
    drained.append(head)
    for _ in range(9):
        nxt = await backend.finish_and_drain(
            "s1", drained[-1].id, "completed",
        )
        if nxt is not None:
            drained.append(nxt)
    assert [e.position for e in drained] == [e.position for e in enqueued]
    assert [e.message for e in drained] == [e.message for e in enqueued]


# ─── merge_or_enqueue ─────────────────────────────────────────────────


async def test_merge_within_window(backend):
    e1, merged = await backend.merge_or_enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="first part", window_seconds=5.0,
    )
    assert merged is False

    e2, merged = await backend.merge_or_enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="second part", window_seconds=5.0,
    )
    assert merged is True
    assert e2.id == e1.id
    assert "first part" in e2.message
    assert "second part" in e2.message


async def test_merge_falls_through_outside_window(backend):
    e1, merged = await backend.merge_or_enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="first", window_seconds=0.05,
    )
    assert merged is False

    await asyncio.sleep(0.2)

    e2, merged = await backend.merge_or_enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="second", window_seconds=0.05,
    )
    assert merged is False
    assert e2.id != e1.id


# ─── replace_last_or_enqueue ──────────────────────────────────────────


async def test_replace_last_busts_old_awaiter(backend):
    from digitorn.core.app.queue_redis import (
        awaiter_future, _awaiters,
    )

    e1, replaced = await backend.replace_last_or_enqueue(
        app_id="a", session_id="s1", user_id="u", message="first",
    )
    assert replaced is False
    fut = awaiter_future(e1.correlation_id)

    e2, replaced = await backend.replace_last_or_enqueue(
        app_id="a", session_id="s1", user_id="u", message="replacement",
    )
    assert replaced is True
    assert e2.id == e1.id  # same row
    assert e2.correlation_id != e1.correlation_id  # fresh corr id

    # Old awaiter should be failed.
    with pytest.raises(RuntimeError, match="replaced"):
        await fut


# ─── Cancel / depth / has_running ─────────────────────────────────────


async def test_cancel_queued_row(backend):
    e1 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u", message="m",
    )
    cancelled = await backend.cancel("s1", e1.id)
    assert cancelled is True
    head = await backend.next_queued("s1")
    assert head is None  # nothing left


async def test_cancel_running_row_refused(backend):
    e1 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u", message="m",
    )
    await backend.next_queued("s1")  # marks e1 running
    cancelled = await backend.cancel("s1", e1.id)
    # Already running - script removes from zset (no-op since it's not
    # there) and returns 0.
    assert cancelled is False


async def test_depth_and_has_running(backend):
    assert await backend.depth_for_session("s1") == 0
    assert await backend.has_running("s1") is False

    await backend.enqueue(
        app_id="a", session_id="s1", user_id="u", message="m1",
    )
    assert await backend.depth_for_session("s1") == 1
    assert await backend.has_running("s1") is False

    await backend.next_queued("s1")
    assert await backend.depth_for_session("s1") == 1
    assert await backend.has_running("s1") is True


# ─── Queue full ────────────────────────────────────────────────────────


async def test_max_depth_enforced(backend):
    for i in range(3):
        await backend.enqueue(
            app_id="a", session_id="s1", user_id="u",
            message=f"m{i}", max_depth=3,
        )
    with pytest.raises(QueueFullError):
        await backend.enqueue(
            app_id="a", session_id="s1", user_id="u",
            message="overflow", max_depth=3,
        )


# ─── Rehydrate on boot ────────────────────────────────────────────────


async def test_rehydrate_recovers_orphaned_running(backend, redis_client):
    """Pretend the daemon crashed mid-turn: a row's hash says
    status=running, the survival pointer queue:was_running:{sid}
    points at it, but the lease marker has expired (or we delete it
    manually). rehydrate_on_boot must resurrect it as queued."""
    e1 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u", message="orphan",
    )
    started = await backend.next_queued("s1")
    assert started.id == e1.id

    # Simulate crash: nuke the running marker (lease expired) but leave
    # everything else behind.
    await redis_client.delete(f"queue:running:s1")

    n = await backend.rehydrate_on_boot()
    assert n == 1

    # Now a fresh next_queued should pick it up.
    revived = await backend.next_queued("s1")
    assert revived is not None
    assert revived.id == e1.id
    assert revived.status == "running"


# ─── TTL enforcement ──────────────────────────────────────────────────


async def test_expired_row_skipped_at_dequeue(backend, redis_client):
    """A row whose ttl_expires_at_unix is in the past must NOT be
    dispatched. It's flipped to ``failed`` with error_code='queue_ttl_expired'
    and the next non-expired row is returned instead."""
    # Enqueue with a very short TTL.
    e1 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="will-expire", ttl_seconds=1,
    )
    # Force the row's ttl into the past by rewriting its hash.
    import time as _t
    past_unix = str(_t.time() - 100)
    await redis_client.hset(
        f"queue:msg:{e1.id}", "ttl_expires_at_unix", past_unix,
    )

    # Enqueue a fresh row that should NOT expire.
    e2 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="alive", ttl_seconds=3600,
    )

    head = await backend.next_queued("s1")
    assert head is not None
    assert head.id == e2.id  # e1 was skipped

    # Verify e1 was marked failed in the audit hash.
    status = await redis_client.hget(f"queue:msg:{e1.id}", "status")
    error = await redis_client.hget(f"queue:msg:{e1.id}", "error_code")
    assert status == b"failed"
    assert error == b"queue_ttl_expired"


async def test_finish_and_drain_skips_expired(backend, redis_client):
    """Same TTL skipping applies inside finish_and_drain's drain phase."""
    e1 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="t1", ttl_seconds=3600,
    )
    started = await backend.next_queued("s1")
    assert started.id == e1.id

    # Enqueue T2 with short TTL, manually expire it.
    e2 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="t2-expired", ttl_seconds=1,
    )
    import time as _t
    await redis_client.hset(
        f"queue:msg:{e2.id}", "ttl_expires_at_unix", str(_t.time() - 100),
    )
    # Enqueue T3 normally.
    e3 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u",
        message="t3-alive", ttl_seconds=3600,
    )

    # Finish T1 and drain - must skip T2 and dispatch T3.
    drained = await backend.finish_and_drain("s1", e1.id, "completed")
    assert drained is not None
    assert drained.id == e3.id

    status = await redis_client.hget(f"queue:msg:{e2.id}", "status")
    assert status == b"failed"


# ─── Real-world scenario: rapid client send during turn end ───────────


async def test_no_orphan_when_message_arrives_just_as_turn_ends(backend):
    """Reproduces the bug we're fixing: T1 finishes, client immediately
    sends T2, but the daemon needs a few ms to mark T1 done. Without
    Redis atomicity, T2 lands between mark_done and next_queued and
    sits forever. With Redis atomicity, T2 is always picked up - either
    directly by finish_and_drain or by a follow-up next_queued."""
    e1 = await backend.enqueue(
        app_id="a", session_id="s1", user_id="u", message="t1",
    )
    started = await backend.next_queued("s1")
    assert started.id == e1.id

    # Client perceives stream finished, fires T2 right when finish_and_drain runs.
    async def race():
        results: dict = {}
        async def t2_post():
            # Pretend tiny latency before T2 enqueue.
            await asyncio.sleep(0)
            results["t2"] = await backend.enqueue(
                app_id="a", session_id="s1", user_id="u",
                message="t2",
            )
        async def t1_finish():
            await asyncio.sleep(0)
            results["drained"] = await backend.finish_and_drain(
                "s1", e1.id, "completed",
            )
        await asyncio.gather(t2_post(), t1_finish())
        return results

    r = await race()
    t2 = r["t2"]
    drained = r["drained"]

    if drained is not None:
        # Atomic Lua picked T2 up directly.
        assert drained.id == t2.id
        # No further drain needed.
    else:
        # T2 enqueue happened AFTER the script's ZPOPMIN. The session
        # currently has T2 queued and no running marker. The producer's
        # post-enqueue self-drain (next_queued) must dispatch it.
        assert await backend.has_running("s1") is False
        followup = await backend.next_queued("s1")
        assert followup is not None
        assert followup.id == t2.id
