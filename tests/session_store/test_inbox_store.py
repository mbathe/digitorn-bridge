"""FileInboxStore: per-user file-based inbox notifications."""
from __future__ import annotations

from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.inbox_store import (
    FileInboxStore, InboxItem,
)


@pytest.mark.asyncio
async def test_add_and_get_roundtrip(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    item = await store.add(
        user_id="u1", kind="session.completed",
        title="Chat finished", subtitle="3 turns",
        app_id="my-app", session_id="s1",
        item_metadata={"duration_s": 12},
    )
    fetched = await store.get(user_id="u1", item_id=item.id)
    assert fetched is not None
    assert fetched.kind == "session.completed"
    assert fetched.title == "Chat finished"
    assert fetched.app_id == "my-app"
    assert fetched.item_metadata == {"duration_s": 12}
    assert fetched.read_at is None
    assert fetched.archived_at is None


@pytest.mark.asyncio
async def test_list_per_user_only(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    await store.add(user_id="alice", kind="x", title="A1")
    await store.add(user_id="alice", kind="x", title="A2")
    await store.add(user_id="bob", kind="x", title="B1")
    a = await store.list(user_id="alice")
    b = await store.list(user_id="bob")
    assert len(a) == 2
    assert len(b) == 1
    assert {it.title for it in a} == {"A1", "A2"}


@pytest.mark.asyncio
async def test_list_unread_only(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    i1 = await store.add(user_id="u", kind="x", title="t1")
    i2 = await store.add(user_id="u", kind="x", title="t2")
    await store.mark_read(user_id="u", item_id=i1.id)
    items = await store.list(user_id="u", unread_only=True)
    assert len(items) == 1
    assert items[0].id == i2.id


@pytest.mark.asyncio
async def test_archived_excluded_by_default(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    i1 = await store.add(user_id="u", kind="x", title="t1")
    i2 = await store.add(user_id="u", kind="x", title="t2")
    await store.archive(user_id="u", item_id=i1.id)
    items = await store.list(user_id="u")
    assert len(items) == 1
    assert items[0].id == i2.id
    items_all = await store.list(user_id="u", include_archived=True)
    assert len(items_all) == 2


@pytest.mark.asyncio
async def test_mark_read_sets_timestamp(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    i = await store.add(user_id="u", kind="x", title="t")
    ok = await store.mark_read(user_id="u", item_id=i.id)
    assert ok is True
    refreshed = await store.get(user_id="u", item_id=i.id)
    assert refreshed.read_at is not None


@pytest.mark.asyncio
async def test_mark_unread(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    i = await store.add(user_id="u", kind="x", title="t")
    await store.mark_read(user_id="u", item_id=i.id)
    await store.mark_unread(user_id="u", item_id=i.id)
    refreshed = await store.get(user_id="u", item_id=i.id)
    assert refreshed.read_at is None


@pytest.mark.asyncio
async def test_archive_implicitly_marks_read(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    i = await store.add(user_id="u", kind="x", title="t")
    await store.archive(user_id="u", item_id=i.id)
    refreshed = await store.get(user_id="u", item_id=i.id)
    assert refreshed.archived_at is not None
    assert refreshed.read_at is not None


@pytest.mark.asyncio
async def test_unarchive(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    i = await store.add(user_id="u", kind="x", title="t")
    await store.archive(user_id="u", item_id=i.id)
    await store.unarchive(user_id="u", item_id=i.id)
    refreshed = await store.get(user_id="u", item_id=i.id)
    assert refreshed.archived_at is None


@pytest.mark.asyncio
async def test_delete_removes_file(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    i = await store.add(user_id="u", kind="x", title="t")
    ok = await store.delete(user_id="u", item_id=i.id)
    assert ok is True
    assert await store.get(user_id="u", item_id=i.id) is None


@pytest.mark.asyncio
async def test_count_unread(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    for n in range(5):
        await store.add(user_id="u", kind="x", title=f"t{n}")
    items = await store.list(user_id="u")
    await store.mark_read(user_id="u", item_id=items[0].id)
    await store.archive(user_id="u", item_id=items[1].id)
    n = await store.count_unread(user_id="u")
    assert n == 3


@pytest.mark.asyncio
async def test_list_sorted_newest_first(tmp_root: Path):
    """list returns newest items first based on created_at."""
    import asyncio
    store = FileInboxStore(root=tmp_root)
    i1 = await store.add(user_id="u", kind="x", title="first")
    await asyncio.sleep(0.005)
    i2 = await store.add(user_id="u", kind="x", title="second")
    await asyncio.sleep(0.005)
    i3 = await store.add(user_id="u", kind="x", title="third")
    items = await store.list(user_id="u")
    assert [it.title for it in items] == ["third", "second", "first"]


@pytest.mark.asyncio
async def test_list_limit(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    for n in range(20):
        await store.add(user_id="u", kind="x", title=f"t{n}")
    items = await store.list(user_id="u", limit=5)
    assert len(items) == 5


@pytest.mark.asyncio
async def test_get_unknown_returns_none(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    assert await store.get(user_id="u", item_id="nope") is None


@pytest.mark.asyncio
async def test_corrupt_file_skipped_in_list(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    await store.add(user_id="u", kind="x", title="ok")
    user_dir = tmp_root / "u"
    (user_dir / "broken.json").write_text("not json", encoding="utf-8")
    items = await store.list(user_id="u")
    assert len(items) == 1
    assert items[0].title == "ok"


@pytest.mark.asyncio
async def test_invalid_user_id_rejected(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    with pytest.raises(ValueError, match="invalid user_id"):
        await store.add(user_id="../etc", kind="x", title="t")
    with pytest.raises(ValueError, match="invalid user_id"):
        await store.add(user_id="", kind="x", title="t")


@pytest.mark.asyncio
async def test_list_unknown_user_empty(tmp_root: Path):
    store = FileInboxStore(root=tmp_root)
    assert await store.list(user_id="ghost") == []
    assert await store.count_unread(user_id="ghost") == 0
