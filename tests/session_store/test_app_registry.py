"""FileAppRegistry: applications + bundles metadata on local disk."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from digitorn.core.runtime.session_store.app_registry import (
    AppBundle, Application, FileAppRegistry,
)


@pytest.mark.asyncio
async def test_register_then_get_roundtrip(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    app = await reg.register_app(
        app_id="my-app", name="My App", version="1.2.3",
        description="An app", author="me", tags=["dev", "test"],
    )
    assert app.app_id == "my-app"
    assert app.scope == "system"
    assert app.owner_user_id == ""
    assert app.tags == ["dev", "test"]
    fetched = await reg.get_app(app_id="my-app")
    assert fetched is not None
    assert fetched.id == app.id


@pytest.mark.asyncio
async def test_user_scope_isolated(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    a_alice = await reg.register_app(
        app_id="my-app", scope="user", owner_user_id="alice", name="A",
    )
    a_bob = await reg.register_app(
        app_id="my-app", scope="user", owner_user_id="bob", name="B",
    )
    a_sys = await reg.register_app(
        app_id="my-app", scope="system", name="S",
    )
    assert a_alice.id != a_bob.id != a_sys.id
    assert (await reg.get_app(
        app_id="my-app", scope="user", owner_user_id="alice",
    )).name == "A"
    assert (await reg.get_app(
        app_id="my-app", scope="user", owner_user_id="bob",
    )).name == "B"
    assert (await reg.get_app(app_id="my-app")).name == "S"


@pytest.mark.asyncio
async def test_register_idempotent_merges(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    a1 = await reg.register_app(app_id="app", name="v1", version="1.0")
    import asyncio
    await asyncio.sleep(0.01)
    a2 = await reg.register_app(app_id="app", name="v2", version="2.0")
    assert a1.id == a2.id
    assert a2.name == "v2"
    assert a2.version == "2.0"
    assert a2.updated_at >= a1.updated_at


@pytest.mark.asyncio
async def test_list_filters_scope_and_user(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="sys-1", name="s1")
    await reg.register_app(app_id="sys-2", name="s2")
    await reg.register_app(
        app_id="alice-1", scope="user", owner_user_id="alice",
    )
    await reg.register_app(
        app_id="bob-1", scope="user", owner_user_id="bob",
    )

    all_apps = await reg.list_apps()
    assert len(all_apps) == 4

    sys_only = await reg.list_apps(scope="system")
    assert {a.app_id for a in sys_only} == {"sys-1", "sys-2"}

    alice_only = await reg.list_apps(scope="user", owner_user_id="alice")
    assert [a.app_id for a in alice_only] == ["alice-1"]

    user_all = await reg.list_apps(scope="user")
    assert {a.app_id for a in user_all} == {"alice-1", "bob-1"}


@pytest.mark.asyncio
async def test_list_excludes_disabled_by_default(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="ok", name="ok")
    await reg.register_app(app_id="bad", name="bad")
    await reg.disable_app(app_id="bad")
    apps = await reg.list_apps()
    assert {a.app_id for a in apps} == {"ok"}
    apps_all = await reg.list_apps(include_disabled=True)
    assert {a.app_id for a in apps_all} == {"ok", "bad"}


@pytest.mark.asyncio
async def test_disable_then_enable(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="x")
    assert await reg.disable_app(app_id="x") is True
    fetched = await reg.get_app(app_id="x")
    assert fetched.disabled is True
    assert fetched.disabled_at is not None
    assert await reg.enable_app(app_id="x") is True
    fetched = await reg.get_app(app_id="x")
    assert fetched.disabled is False
    assert fetched.disabled_at is None


@pytest.mark.asyncio
async def test_update_app_only_known_fields(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="orig", version="1.0")
    updated = await reg.update_app(
        app_id="x", name="renamed", bogus_field="ignored",
    )
    assert updated is not None
    assert updated.name == "renamed"
    assert updated.version == "1.0"
    assert not hasattr(updated, "bogus_field")


@pytest.mark.asyncio
async def test_update_unknown_returns_none(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    assert await reg.update_app(app_id="ghost", name="x") is None


@pytest.mark.asyncio
async def test_delete_app(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="x")
    assert await reg.delete_app(app_id="x") is True
    assert await reg.get_app(app_id="x") is None
    assert await reg.delete_app(app_id="x") is False


@pytest.mark.asyncio
async def test_register_bundle_sets_current(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="x")
    b1 = await reg.register_bundle(
        app_id="x", bundle_hash="abc", bundle_path="/path/b1",
        asset_count=3, size_bytes=1024,
    )
    app = await reg.get_app(app_id="x")
    assert app.current_bundle_id == b1.id


@pytest.mark.asyncio
async def test_register_bundle_no_set_current(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="x")
    b1 = await reg.register_bundle(
        app_id="x", bundle_hash="abc", bundle_path="/p",
    )
    b2 = await reg.register_bundle(
        app_id="x", bundle_hash="def", bundle_path="/p2",
        set_current=False,
    )
    app = await reg.get_app(app_id="x")
    assert app.current_bundle_id == b1.id


@pytest.mark.asyncio
async def test_list_bundles_per_app(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="x")
    await reg.register_bundle(
        app_id="x", bundle_hash="h1", bundle_path="/p1",
    )
    await reg.register_bundle(
        app_id="x", bundle_hash="h2", bundle_path="/p2",
    )
    bundles = await reg.list_bundles(app_id="x")
    assert len(bundles) == 2
    assert {b.bundle_hash for b in bundles} == {"h1", "h2"}


@pytest.mark.asyncio
async def test_get_bundle(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="x")
    b = await reg.register_bundle(
        app_id="x", bundle_hash="h", bundle_path="/p",
    )
    fetched = await reg.get_bundle(app_id="x", bundle_id=b.id)
    assert fetched is not None
    assert fetched.id == b.id
    assert await reg.get_bundle(app_id="x", bundle_id="nope") is None


@pytest.mark.asyncio
async def test_bundles_isolated_by_scope(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="sys")
    await reg.register_app(
        app_id="x", scope="user", owner_user_id="alice", name="alice",
    )
    await reg.register_bundle(
        app_id="x", bundle_hash="sys-bundle", bundle_path="/sys",
    )
    await reg.register_bundle(
        app_id="x", scope="user", owner_user_id="alice",
        bundle_hash="alice-bundle", bundle_path="/alice",
    )
    sys_bundles = await reg.list_bundles(app_id="x")
    alice_bundles = await reg.list_bundles(
        app_id="x", scope="user", owner_user_id="alice",
    )
    assert len(sys_bundles) == 1
    assert sys_bundles[0].bundle_hash == "sys-bundle"
    assert len(alice_bundles) == 1
    assert alice_bundles[0].bundle_hash == "alice-bundle"


@pytest.mark.asyncio
async def test_invalid_app_id_rejected(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    with pytest.raises(ValueError, match="invalid app_id"):
        await reg.register_app(app_id="../escape")
    with pytest.raises(ValueError, match="invalid app_id"):
        await reg.register_app(app_id="bad/slash")
    with pytest.raises(ValueError, match="invalid app_id"):
        await reg.register_app(app_id="")


@pytest.mark.asyncio
async def test_invalid_owner_rejected(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    with pytest.raises(ValueError, match="invalid owner_user_id"):
        await reg.register_app(
            app_id="x", scope="user", owner_user_id="../escape",
        )


@pytest.mark.asyncio
async def test_corrupt_application_returns_none(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    sys_dir = tmp_root / "system" / "x"
    sys_dir.mkdir(parents=True)
    (sys_dir / "application.json").write_text("garbage", encoding="utf-8")
    assert await reg.get_app(app_id="x") is None


@pytest.mark.asyncio
async def test_corrupt_bundles_returns_empty(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="x")
    bundles_path = tmp_root / "system" / "x" / "bundles.json"
    bundles_path.write_text("garbage", encoding="utf-8")
    assert await reg.list_bundles(app_id="x") == []


@pytest.mark.asyncio
async def test_get_unknown_app_returns_none(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    assert await reg.get_app(app_id="ghost") is None
    assert await reg.list_bundles(app_id="ghost") == []


@pytest.mark.asyncio
async def test_atomic_write_no_partial_files(tmp_root: Path):
    reg = FileAppRegistry(root=tmp_root)
    await reg.register_app(app_id="x", name="x")
    sys_dir = tmp_root / "system" / "x"
    files = sorted(p.name for p in sys_dir.iterdir())
    assert files == ["application.json"]
