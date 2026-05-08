"""SeqAllocator tests: monotonicity, threading safety, cold-seed."""
from __future__ import annotations

import threading

import pytest

from digitorn.core.runtime.session_store.seq_allocator import SeqAllocator


def test_monotonic_per_session():
    a = SeqAllocator(seed_loader=lambda _k: 0)
    seqs = [a.next(session_id="s1") for _ in range(10)]
    assert seqs == list(range(1, 11))


def test_per_session_isolation():
    a = SeqAllocator(seed_loader=lambda _k: 0)
    s1 = [a.next(session_id="s1") for _ in range(5)]
    s2 = [a.next(session_id="s2") for _ in range(5)]
    assert s1 == [1, 2, 3, 4, 5]
    assert s2 == [1, 2, 3, 4, 5]


def test_user_scope_separate_from_session_scope():
    a = SeqAllocator(seed_loader=lambda _k: 0)
    a.next(session_id="abc")
    a.next(session_id="abc")
    n = a.next(user_id="u1")
    assert n == 1


def test_seed_loader_called_once_per_scope():
    calls: list[str] = []
    def loader(k: str) -> int:
        calls.append(k)
        return 100

    a = SeqAllocator(seed_loader=loader)
    s = [a.next(session_id="s1") for _ in range(3)]
    assert s == [101, 102, 103]
    assert calls == ["session::s1"]


def test_seed_failure_defaults_to_zero():
    def loader(_k: str) -> int:
        raise RuntimeError("disk unreachable")

    a = SeqAllocator(seed_loader=loader)
    n = a.next(session_id="s1")
    assert n == 1


def test_latest_does_not_increment():
    a = SeqAllocator(seed_loader=lambda _k: 5)
    a.next(session_id="s1")
    a.next(session_id="s1")
    assert a.latest(session_id="s1") == 7
    assert a.latest(session_id="s1") == 7
    assert a.next(session_id="s1") == 8


def test_threadsafe_no_dupes_no_gaps():
    """100 threads x 100 allocations: 10000 unique consecutive seqs."""
    a = SeqAllocator(seed_loader=lambda _k: 0)
    results: list[int] = []
    lock = threading.Lock()

    def worker():
        local = []
        for _ in range(100):
            local.append(a.next(session_id="hot"))
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 10_000
    assert len(set(results)) == 10_000
    assert sorted(results) == list(range(1, 10_001))


def test_force_seed_overrides():
    a = SeqAllocator(seed_loader=lambda _k: 0)
    a.force_seed("session::s1", 42)
    assert a.next(session_id="s1") == 43


def test_reset_for_tests():
    a = SeqAllocator(seed_loader=lambda _k: 0)
    a.next(session_id="s1")
    a.next(session_id="s1")
    a.reset_for_tests()
    assert a.next(session_id="s1") == 1
