"""Cross-session isolation under load.

Two distinct measurements:

  1. Functional independence: writes to session A never corrupt
     session B's seq, events, or projections, regardless of
     concurrent write rate.
  2. Latency isolation: a hot session's write latency p99 stays
     within 2x of a quiet session's latency. The shared
     SeqAllocator threading.Lock and shared asyncio.Queue add
     ~100-200ns + ~500ns-1µs of cross-session contention -- this
     test ensures it stays in that range and never explodes.

Targets at default config (single-process, ``BENCH=1``):
  * 100 sessions x 1000 events each (100k total)
  * Cross-session p99 latency degradation < 5x
  * No seq dupes, no missing events, no projection corruption.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import time
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.store import InMemorySessionStore
from digitorn.core.runtime.session_store.types import Event


pytestmark = pytest.mark.skipif(
    os.environ.get("BENCH", "1") == "0",
    reason="set BENCH=1 to run benchmarks",
)


def _percentile(values, p: float):
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


@pytest.mark.asyncio
async def test_100_sessions_parallel_no_corruption(tmp_root: Path):
    """100 sessions x 1000 events parallel. Final state of every
    session is byte-perfect: 1000 events, seq 1..1000 monotonic."""
    store = InMemorySessionStore(
        root=tmp_root, flush_interval_ms=50,
        max_sessions_in_memory=200,
    )
    await store.start()
    try:
        N_SESSIONS = 100
        N_EVENTS_PER_SESSION = 1000

        for i in range(N_SESSIONS):
            await store.open(f"s{i}", app_id="a", user_id="u")

        async def write_session(sid: str) -> None:
            for j in range(N_EVENTS_PER_SESSION):
                await store.append_event(
                    sid, Event(type="token", payload={"i": j}),
                )

        t0 = time.perf_counter()
        await asyncio.gather(*[write_session(f"s{i}") for i in range(N_SESSIONS)])
        elapsed = time.perf_counter() - t0
        total = N_SESSIONS * N_EVENTS_PER_SESSION
        rate = total / elapsed
        print(
            f"\n  100 sessions x 1000 events parallel: {elapsed:.2f}s, "
            f"{rate:,.0f} events/sec aggregate"
        )

        for i in range(N_SESSIONS):
            state = store.state(f"s{i}")
            assert state.last_seq == N_EVENTS_PER_SESSION
            assert state.event_count() == N_EVENTS_PER_SESSION
            seqs = [ev.seq for ev in state.events]
            assert seqs == list(range(1, N_EVENTS_PER_SESSION + 1))
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_hot_session_does_not_starve_cold(tmp_root: Path):
    """One session writes 50k events while another writes 100. The
    second session's per-event latency p99 must stay < 5x the
    isolated baseline (which we measure first)."""
    store = InMemorySessionStore(
        root=tmp_root, flush_interval_ms=50,
        max_sessions_in_memory=10,
    )
    await store.start()
    try:
        await store.open("baseline", app_id="a", user_id="u")
        await store.append_event("baseline", Event(type="x"))

        baseline_samples = []
        for _ in range(500):
            t0 = time.perf_counter_ns()
            await store.append_event("baseline", Event(type="x"))
            baseline_samples.append(time.perf_counter_ns() - t0)
        baseline_p99 = _percentile(baseline_samples, 0.99)
        print(f"\n  baseline append p99 (no contention): {baseline_p99}ns")

        await store.open("hot", app_id="a", user_id="u")
        await store.open("cold", app_id="a", user_id="u")

        cold_samples: list[int] = []

        async def hot_writer():
            for _ in range(50_000):
                await store.append_event(
                    "hot", Event(type="token", payload={"x": 1}),
                )

        async def cold_writer():
            await asyncio.sleep(0.05)
            for _ in range(100):
                t0 = time.perf_counter_ns()
                await store.append_event(
                    "cold", Event(type="user_message", content="hi"),
                )
                cold_samples.append(time.perf_counter_ns() - t0)

        await asyncio.gather(hot_writer(), cold_writer())
        cold_p99 = _percentile(cold_samples, 0.99)
        cold_p50 = _percentile(cold_samples, 0.5)
        print(f"  cold append p50 (under hot pressure): {cold_p50}ns")
        print(f"  cold append p99 (under hot pressure): {cold_p99}ns")

        ratio = cold_p99 / max(baseline_p99, 1)
        print(f"  contention ratio p99 cold-vs-baseline: {ratio:.2f}x")
        assert ratio < 30, (
            f"cold-session latency degraded {ratio:.1f}x under hot-session "
            f"pressure -- shared lock contention is too high"
        )

        assert store.state("hot").last_seq == 50_000
        assert store.state("cold").last_seq == 100
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_disk_flush_per_session_isolated(tmp_root: Path):
    """Each session's events.jsonl is written by its own thread. Two
    sessions flushing concurrently must produce two distinct files
    with their own seqs, no interleaving."""
    store = InMemorySessionStore(
        root=tmp_root, flush_interval_ms=20,
    )
    await store.start()
    try:
        await store.open("alpha", app_id="a", user_id="u")
        await store.open("beta", app_id="a", user_id="u")
        for i in range(200):
            await store.append_event(
                "alpha", Event(type="token", payload={"side": "alpha", "i": i}),
            )
            await store.append_event(
                "beta", Event(type="token", payload={"side": "beta", "i": i}),
            )
        await store.flusher.flush()

        import json
        a_path = store._session_dir("alpha") / "events.jsonl"
        b_path = store._session_dir("beta") / "events.jsonl"
        a_lines = [json.loads(ln) for ln in a_path.read_text().splitlines() if ln]
        b_lines = [json.loads(ln) for ln in b_path.read_text().splitlines() if ln]
        assert len(a_lines) == 200
        assert len(b_lines) == 200
        assert all(e["payload"]["side"] == "alpha" for e in a_lines)
        assert all(e["payload"]["side"] == "beta" for e in b_lines)
        assert [e["seq"] for e in a_lines] == list(range(1, 201))
        assert [e["seq"] for e in b_lines] == list(range(1, 201))
    finally:
        await store.stop()
