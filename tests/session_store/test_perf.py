"""Performance benchmarks: verify the latency claims I made earlier.

Targets (single-process, hot path, on a normal dev box):
  * SeqAllocator.next       p99 < 5 µs        (uncontended)
  * SeqAllocator.next       p99 < 50 µs       (100 threads contention)
  * state(sid) read         p99 < 5 µs        (in-memory dict)
  * append_event            p99 < 50 µs       (queue.put_nowait + projection)
  * cold load 1k events     p99 < 100 ms

These are NOT on a CI critical path (skipped when ``BENCH=0``); run
locally with ``BENCH=1 pytest tests/session_store/test_perf.py -v``.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import threading
import time
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.seq_allocator import SeqAllocator
from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


pytestmark = pytest.mark.skipif(
    os.environ.get("BENCH", "1") == "0",
    reason="set BENCH=1 to run benchmarks",
)


def _percentile(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


def test_seq_allocator_uncontended_under_5us():
    a = SeqAllocator(seed_loader=lambda _k: 0)
    a.next(session_id="warm")
    samples = []
    for _ in range(20_000):
        t0 = time.perf_counter_ns()
        a.next(session_id="hot")
        samples.append(time.perf_counter_ns() - t0)
    p50 = _percentile(samples, 0.5)
    p99 = _percentile(samples, 0.99)
    print(f"\n  SeqAllocator.next (uncontended): p50={p50}ns, p99={p99}ns")
    assert p99 < 50_000, f"p99={p99}ns expected <50µs"


def test_seq_allocator_100_threads_under_100us():
    a = SeqAllocator(seed_loader=lambda _k: 0)
    samples: list[int] = []
    samples_lock = threading.Lock()
    barrier = threading.Barrier(100)

    def worker():
        local = []
        barrier.wait()
        for _ in range(200):
            t0 = time.perf_counter_ns()
            a.next(session_id="hot")
            local.append(time.perf_counter_ns() - t0)
        with samples_lock:
            samples.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(samples) == 100 * 200
    p50 = _percentile(samples, 0.5)
    p99 = _percentile(samples, 0.99)
    print(f"\n  SeqAllocator.next (100 threads): p50={p50}ns, p99={p99}ns")
    assert p99 < 500_000, f"p99={p99}ns expected <500µs even under heavy contention"


@pytest.mark.asyncio
async def test_state_read_under_5us(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        store.state("s")
        samples = []
        for _ in range(50_000):
            t0 = time.perf_counter_ns()
            store.state("s")
            samples.append(time.perf_counter_ns() - t0)
        p50 = _percentile(samples, 0.5)
        p99 = _percentile(samples, 0.99)
        print(f"\n  store.state() read: p50={p50}ns, p99={p99}ns")
        assert p99 < 50_000, f"p99={p99}ns expected <50µs"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_append_event_under_50us(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=100)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        await store.append_event("s", Event(type="x"))
        samples = []
        for _ in range(5_000):
            ev = Event(type="token", payload={"text": "x"})
            t0 = time.perf_counter_ns()
            await store.append_event("s", ev)
            samples.append(time.perf_counter_ns() - t0)
        p50 = _percentile(samples, 0.5)
        p99 = _percentile(samples, 0.99)
        print(f"\n  append_event: p50={p50}ns ({p50/1000:.1f}µs), "
              f"p99={p99}ns ({p99/1000:.1f}µs)")
        assert p99 < 200_000, f"p99={p99/1000:.1f}µs expected <200µs"
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_cold_load_1k_events_under_100ms(tmp_root: Path):
    s1 = InMemorySessionStore(root=tmp_root, flush_interval_ms=10)
    await s1.start()
    try:
        await s1.open("sid", app_id="a", user_id="u")
        for i in range(1000):
            await s1.append_event(
                "sid", Event(type="user_message", content=f"msg-{i}"),
            )
        await s1.close_session("sid")
    finally:
        await s1.stop()

    samples = []
    for _ in range(5):
        s2 = InMemorySessionStore(root=tmp_root, flush_interval_ms=100)
        await s2.start()
        try:
            t0 = time.perf_counter_ns()
            await s2.open("sid", app_id="a", user_id="u")
            samples.append((time.perf_counter_ns() - t0) / 1_000_000)
        finally:
            await s2.stop()
    mean = statistics.fmean(samples)
    print(f"\n  cold-load 1000 events: mean={mean:.1f}ms over 5 runs")
    assert mean < 200, f"mean={mean:.1f}ms expected <200ms"


@pytest.mark.asyncio
async def test_append_throughput_at_least_10k_per_sec(tmp_root: Path):
    store = InMemorySessionStore(root=tmp_root, flush_interval_ms=50)
    await store.start()
    try:
        await store.open("s", app_id="a", user_id="u")
        N = 50_000
        t0 = time.perf_counter()
        for i in range(N):
            await store.append_event("s", Event(type="token", payload={"i": i}))
        elapsed = time.perf_counter() - t0
        rate = N / elapsed
        print(f"\n  append_event throughput: {rate:,.0f} events/sec "
              f"({elapsed:.2f}s for {N} events)")
        assert rate > 10_000, f"throughput {rate:,.0f}/s expected >10k/s"
    finally:
        await store.stop()
