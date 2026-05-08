"""SqliteSessionIndex: cross-session queries by user, app, time, parent."""
from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.session_index import (
    SessionSummary, SqliteSessionIndex,
)


@pytest.fixture
def index(tmp_root: Path) -> SqliteSessionIndex:
    return SqliteSessionIndex(db_path=tmp_root / "index.db")


def _summary(**overrides) -> SessionSummary:
    base = dict(
        session_id="s1", app_id="myapp", user_id="alice",
        started_at="2026-05-08T10:00:00+00:00",
        last_seq=10, event_count=10,
    )
    base.update(overrides)
    return SessionSummary(**base)


@pytest.mark.asyncio
async def test_upsert_then_get(index: SqliteSessionIndex):
    s = _summary(session_id="abc", title="Test chat")
    await index.upsert(s)
    fetched = await index.get("abc")
    assert fetched is not None
    assert fetched.session_id == "abc"
    assert fetched.title == "Test chat"
    assert fetched.user_id == "alice"
    assert fetched.last_seq == 10


@pytest.mark.asyncio
async def test_upsert_overwrites(index: SqliteSessionIndex):
    await index.upsert(_summary(session_id="abc", last_seq=10))
    await index.upsert(_summary(session_id="abc", last_seq=42))
    fetched = await index.get("abc")
    assert fetched.last_seq == 42


@pytest.mark.asyncio
async def test_get_unknown_returns_none(index: SqliteSessionIndex):
    assert await index.get("nope") is None


@pytest.mark.asyncio
async def test_list_for_user_orders_newest_first(
    index: SqliteSessionIndex,
):
    await index.upsert(_summary(
        session_id="s1", started_at="2026-05-01T00:00:00+00:00",
    ))
    await index.upsert(_summary(
        session_id="s2", started_at="2026-05-08T00:00:00+00:00",
    ))
    await index.upsert(_summary(
        session_id="s3", started_at="2026-05-04T00:00:00+00:00",
    ))
    sessions = await index.list_for_user("alice")
    assert [s.session_id for s in sessions] == ["s2", "s3", "s1"]


@pytest.mark.asyncio
async def test_list_for_user_filter_by_app(index: SqliteSessionIndex):
    await index.upsert(_summary(session_id="a1", app_id="appA"))
    await index.upsert(_summary(session_id="a2", app_id="appA"))
    await index.upsert(_summary(session_id="b1", app_id="appB"))
    out = await index.list_for_user("alice", app_id="appA")
    assert {s.session_id for s in out} == {"a1", "a2"}


@pytest.mark.asyncio
async def test_list_for_user_filter_by_time(index: SqliteSessionIndex):
    await index.upsert(_summary(
        session_id="old", started_at="2026-04-01T00:00:00+00:00",
    ))
    await index.upsert(_summary(
        session_id="mid", started_at="2026-05-01T00:00:00+00:00",
    ))
    await index.upsert(_summary(
        session_id="new", started_at="2026-06-01T00:00:00+00:00",
    ))
    out = await index.list_for_user(
        "alice",
        since="2026-04-15T00:00:00+00:00",
        until="2026-05-15T00:00:00+00:00",
    )
    assert [s.session_id for s in out] == ["mid"]


@pytest.mark.asyncio
async def test_list_for_user_excludes_children_by_default(
    index: SqliteSessionIndex,
):
    await index.upsert(_summary(session_id="parent"))
    await index.upsert(_summary(
        session_id="child", parent_session_id="parent",
    ))
    out = await index.list_for_user("alice")
    assert {s.session_id for s in out} == {"parent"}
    out_all = await index.list_for_user(
        "alice", include_archived_children=True,
    )
    assert {s.session_id for s in out_all} == {"parent", "child"}


@pytest.mark.asyncio
async def test_list_for_user_pagination(index: SqliteSessionIndex):
    for i in range(20):
        await index.upsert(_summary(
            session_id=f"s{i:02d}",
            started_at=f"2026-05-{i+1:02d}T00:00:00+00:00",
        ))
    page1 = await index.list_for_user("alice", limit=5, offset=0)
    page2 = await index.list_for_user("alice", limit=5, offset=5)
    assert len(page1) == 5
    assert len(page2) == 5
    assert {s.session_id for s in page1} & {s.session_id for s in page2} == set()


@pytest.mark.asyncio
async def test_list_for_user_isolated(index: SqliteSessionIndex):
    await index.upsert(_summary(session_id="a", user_id="alice"))
    await index.upsert(_summary(session_id="b", user_id="bob"))
    a = await index.list_for_user("alice")
    b = await index.list_for_user("bob")
    assert [s.session_id for s in a] == ["a"]
    assert [s.session_id for s in b] == ["b"]


@pytest.mark.asyncio
async def test_list_for_app(index: SqliteSessionIndex):
    await index.upsert(_summary(session_id="a", app_id="x", user_id="alice"))
    await index.upsert(_summary(session_id="b", app_id="x", user_id="bob"))
    await index.upsert(_summary(session_id="c", app_id="y", user_id="alice"))
    out = await index.list_for_app("x")
    assert {s.session_id for s in out} == {"a", "b"}


@pytest.mark.asyncio
async def test_list_children(index: SqliteSessionIndex):
    await index.upsert(_summary(session_id="parent"))
    await index.upsert(_summary(
        session_id="c1", parent_session_id="parent",
        started_at="2026-05-08T10:00:00+00:00",
    ))
    await index.upsert(_summary(
        session_id="c2", parent_session_id="parent",
        started_at="2026-05-08T10:05:00+00:00",
    ))
    children = await index.list_children("parent")
    assert [s.session_id for s in children] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_search_titles(index: SqliteSessionIndex):
    await index.upsert(_summary(
        session_id="s1", title="Bug investigation",
        summary="Fixed a leak in foo.py",
    ))
    await index.upsert(_summary(
        session_id="s2", title="Feature design",
        summary="Discussion on auth flow",
    ))
    await index.upsert(_summary(
        session_id="s3", title="Refactor sprint",
        summary="Bug-free release",
    ))
    out = await index.search_titles("alice", "bug")
    assert {s.session_id for s in out} == {"s1", "s3"}


@pytest.mark.asyncio
async def test_delete(index: SqliteSessionIndex):
    await index.upsert(_summary(session_id="x"))
    assert await index.delete("x") is True
    assert await index.get("x") is None
    assert await index.delete("x") is False


@pytest.mark.asyncio
async def test_count_for_user(index: SqliteSessionIndex):
    for i in range(5):
        await index.upsert(_summary(session_id=f"a{i}", user_id="alice"))
    for i in range(3):
        await index.upsert(_summary(session_id=f"b{i}", user_id="bob"))
    assert await index.count_for_user("alice") == 5
    assert await index.count_for_user("bob") == 3
    assert await index.count_for_user("ghost") == 0


@pytest.mark.asyncio
async def test_rebuild_from_summaries(index: SqliteSessionIndex):
    await index.upsert(_summary(session_id="old1"))
    await index.upsert(_summary(session_id="old2"))
    n = await index.rebuild_from_summaries([
        _summary(session_id="new1"),
        _summary(session_id="new2"),
        _summary(session_id="new3"),
    ])
    assert n == 3
    out = await index.list_for_user("alice")
    assert {s.session_id for s in out} == {"new1", "new2", "new3"}


@pytest.mark.asyncio
async def test_persists_across_instances(tmp_root: Path):
    db = tmp_root / "index.db"
    idx1 = SqliteSessionIndex(db_path=db)
    await idx1.upsert(_summary(session_id="abc"))
    idx2 = SqliteSessionIndex(db_path=db)
    fetched = await idx2.get("abc")
    assert fetched is not None
    assert fetched.session_id == "abc"


@pytest.mark.asyncio
async def test_summary_from_state_summary():
    s = SessionSummary.from_state_summary({
        "session_id": "s1",
        "app_id": "a",
        "user_id": "u",
        "parent_session_id": None,
        "first_seq": 1,
        "last_seq": 42,
        "event_count": 42,
        "started_at": "ts",
        "ended_at": None,
        "closed": False,
        "cost_total": 0.5,
        "tokens_in": 100,
        "tokens_out": 50,
        "child_count": 0,
    })
    assert s.session_id == "s1"
    assert s.last_seq == 42
    assert s.cost_total == 0.5


@pytest.mark.asyncio
async def test_store_close_upserts_index(tmp_root: Path):
    """End-to-end: closing a session via the store auto-upserts its
    summary into the wired index."""
    from digitorn.core.runtime.session_store.store import (
        InMemorySessionStore,
    )
    from digitorn.core.runtime.session_store.types import Event

    index = SqliteSessionIndex(db_path=tmp_root / "idx.db")
    store = InMemorySessionStore(
        root=tmp_root / "sessions",
        flush_interval_ms=10,
        index=index,
    )
    await store.start()
    try:
        await store.open("sid", app_id="myapp", user_id="paul")
        await store.append_event(
            "sid", Event(type="user_message", role="user", content="hi"),
        )
        await store.close_session("sid")

        summary = await index.get("sid")
        assert summary is not None
        assert summary.app_id == "myapp"
        assert summary.user_id == "paul"
        assert summary.last_seq == 1
        assert summary.closed is True
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_store_compact_upserts_index(tmp_root: Path):
    """Compacting also refreshes the index entry so the dashboard
    sees the post-compaction state."""
    from digitorn.core.runtime.session_store.store import (
        InMemorySessionStore,
    )
    from digitorn.core.runtime.session_store.types import Event

    index = SqliteSessionIndex(db_path=tmp_root / "idx.db")
    store = InMemorySessionStore(
        root=tmp_root / "sessions",
        flush_interval_ms=10,
        index=index,
    )
    await store.start()
    try:
        await store.open("sid", app_id="a", user_id="u")
        for i in range(5):
            await store.append_event(
                "sid", Event(type="user_message", content=f"m{i}"),
            )
        await store.compact_session(
            "sid", cutoff_seq=2, summary="recap",
            tokens_estimate=10, model="m",
        )
        summary = await index.get("sid")
        assert summary is not None
        assert summary.last_seq >= 5
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_index_failures_dont_break_close(tmp_root: Path):
    """If the index errors, close_session must still complete."""
    from digitorn.core.runtime.session_store.store import (
        InMemorySessionStore,
    )
    from digitorn.core.runtime.session_store.types import Event

    class _BrokenIndex:
        async def upsert(self, summary):
            raise RuntimeError("simulated failure")

    store = InMemorySessionStore(
        root=tmp_root,
        flush_interval_ms=10,
        index=_BrokenIndex(),
    )
    await store.start()
    try:
        await store.open("sid", app_id="a", user_id="u")
        await store.append_event("sid", Event(type="user_message", content="x"))
        await store.close_session("sid")
        state = store.state("sid")
        assert state.closed is True
        assert state.pinned is False
    finally:
        await store.stop()
