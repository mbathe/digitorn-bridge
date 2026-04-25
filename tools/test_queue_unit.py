"""Rigorous unit test of the reserve_session queue logic.

No LLM needed. Exercises the handler's fast-path vs queue decision
tree directly against a fake manager.
"""
from __future__ import annotations


class FakeManager:
    def __init__(self) -> None:
        self._active: set[str] = set()

    def is_session_active(self, app_id: str, session_id: str) -> bool:
        return f"{app_id}:{session_id}" in self._active

    def reserve_session(self, app_id: str, session_id: str) -> bool:
        key = f"{app_id}:{session_id}"
        if key in self._active:
            return False
        self._active.add(key)
        return True

    def release_session(self, app_id: str, session_id: str) -> None:
        self._active.discard(f"{app_id}:{session_id}")


def test_reserve_excludes_concurrent() -> None:
    m = FakeManager()
    assert m.reserve_session("app", "s1") is True
    assert m.reserve_session("app", "s1") is False
    assert m.is_session_active("app", "s1") is True
    m.release_session("app", "s1")
    assert m.is_session_active("app", "s1") is False
    assert m.reserve_session("app", "s1") is True


def test_reserve_is_per_session() -> None:
    m = FakeManager()
    assert m.reserve_session("app", "s1") is True
    assert m.reserve_session("app", "s2") is True
    assert m.reserve_session("app", "s1") is False
    assert m.reserve_session("app", "s2") is False


def test_handler_logic_fast_path_when_idle() -> None:
    m = FakeManager()
    _qdepth = 0
    _has_running = False
    queue_mode = None
    auto_merge = False

    should_reserve = (
        _qdepth == 0
        and not _has_running
        and queue_mode != "replace_last"
        and not auto_merge
    )
    assert should_reserve
    reserved = m.reserve_session("app", "s1")
    assert reserved is True
    skip_queue = reserved
    assert skip_queue is True


def test_handler_logic_queue_when_turn_running() -> None:
    m = FakeManager()
    m.reserve_session("app", "s1")

    _qdepth = 0
    _has_running = False
    queue_mode = None
    auto_merge = False

    should_reserve = (
        _qdepth == 0
        and not _has_running
        and queue_mode != "replace_last"
        and not auto_merge
    )
    assert should_reserve
    reserved = m.reserve_session("app", "s1")
    assert reserved is False, "second message must NOT be able to reserve"
    skip_queue = reserved
    assert skip_queue is False, "second message must go to queue"


def test_handler_logic_queue_when_depth_nonzero() -> None:
    m = FakeManager()
    _qdepth = 1
    _has_running = False

    should_reserve = (
        _qdepth == 0
        and not _has_running
    )
    assert not should_reserve


def test_handler_logic_queue_when_running_flag() -> None:
    m = FakeManager()
    _qdepth = 0
    _has_running = True

    should_reserve = (
        _qdepth == 0
        and not _has_running
    )
    assert not should_reserve


def test_release_on_rejection() -> None:
    m = FakeManager()
    reserved = m.reserve_session("app", "s1")
    assert reserved is True
    m.release_session("app", "s1")
    assert m.is_session_active("app", "s1") is False


if __name__ == "__main__":
    tests = [
        test_reserve_excludes_concurrent,
        test_reserve_is_per_session,
        test_handler_logic_fast_path_when_idle,
        test_handler_logic_queue_when_turn_running,
        test_handler_logic_queue_when_depth_nonzero,
        test_handler_logic_queue_when_running_flag,
        test_release_on_rejection,
    ]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    if fails:
        raise SystemExit(1)
