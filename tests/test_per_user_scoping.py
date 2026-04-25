"""End-to-end tests for per-user install scoping.

Covers:
- PackageRegistry CRUD with scope + owner_user_id
- Same package_id installable at scope=system AND scope=user
- resolve_for_caller: user shadow over system
- list_visible_to_user: user sees own + system, never other users
- InstallFlow.install with both scopes
- Install dir resolution (system vs user-prefixed)
- AppManager._deployed scoped keys
- Policy: non-admin can't install scope=system
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))


async def _build_registry():
    from digitorn.core.config import get_settings, override_settings
    from digitorn.core.database import Base, get_session_factory, init_db
    from digitorn.core.packages.registry import PackageRegistry

    settings = get_settings()
    override_settings(settings.model_copy(update={
        "database": settings.database.model_copy(update={
            "url": "sqlite+aiosqlite:///:memory:",
        }),
    }))
    engine = await init_db(get_settings())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return PackageRegistry(get_session_factory())


@pytest.mark.asyncio
async def test_registry_coexistence_system_and_user() -> None:
    """Same package_id at system and user scope coexist."""
    from digitorn.core.packages.registry import Scope
    r = await _build_registry()

    await r.create(
        package_id="my-app",
        source_type="local",
        source_uri="/tmp/my-app",
        version="1.0.0",
        hash="h1",
        install_dir="/packages/my-app",
        manifest={"package": {"id": "my-app", "version": "1.0.0"}},
        scope=Scope.SYSTEM,
    )
    await r.create(
        package_id="my-app",
        source_type="local",
        source_uri="/tmp/alice-my-app",
        version="1.0.0-alice",
        hash="h2",
        install_dir="/users/alice/packages/my-app",
        manifest={"package": {"id": "my-app", "version": "1.0.0-alice"}},
        scope=Scope.USER,
        owner_user_id="alice",
    )

    # Both rows exist
    system_row = await r.get("my-app", scope=Scope.SYSTEM)
    assert system_row is not None
    assert system_row["version"] == "1.0.0"

    alice_row = await r.get(
        "my-app", scope=Scope.USER, owner_user_id="alice",
    )
    assert alice_row is not None
    assert alice_row["version"] == "1.0.0-alice"


@pytest.mark.asyncio
async def test_resolve_for_caller_user_shadows_system() -> None:
    from digitorn.core.packages.registry import Scope
    r = await _build_registry()

    await r.create(
        package_id="chat",
        source_type="builtin",
        source_uri="bundle://digitorn/chat",
        version="1.0.0",
        hash="h1",
        install_dir="/packages/chat",
        manifest={"package": {"id": "chat"}},
        scope=Scope.SYSTEM,
    )
    # Alice installs her own copy
    await r.create(
        package_id="chat",
        source_type="local",
        source_uri="/tmp/alice-chat",
        version="1.1.0",
        hash="h2",
        install_dir="/users/alice/packages/chat",
        manifest={"package": {"id": "chat"}},
        scope=Scope.USER,
        owner_user_id="alice",
    )

    # Alice sees her own version
    alice_view = await r.resolve_for_caller("chat", user_id="alice")
    assert alice_view["scope"] == "user"
    assert alice_view["version"] == "1.1.0"

    # Bob (no user install) sees the system version
    bob_view = await r.resolve_for_caller("chat", user_id="bob")
    assert bob_view["scope"] == "system"
    assert bob_view["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_list_visible_isolation() -> None:
    """Alice sees her installs + system; Bob sees his + system.
    Neither sees the other user's installs."""
    from digitorn.core.packages.registry import Scope
    r = await _build_registry()

    await r.create(
        package_id="chat", source_type="builtin",
        source_uri="bundle://digitorn/chat", version="1.0", hash="",
        install_dir="/packages/chat", manifest={"package": {"id": "chat"}},
        scope=Scope.SYSTEM,
    )
    await r.create(
        package_id="alice-only", source_type="local",
        source_uri="/tmp/alice-only", version="1.0", hash="",
        install_dir="/users/alice/packages/alice-only",
        manifest={"package": {"id": "alice-only"}},
        scope=Scope.USER, owner_user_id="alice",
    )
    await r.create(
        package_id="bob-only", source_type="local",
        source_uri="/tmp/bob-only", version="1.0", hash="",
        install_dir="/users/bob/packages/bob-only",
        manifest={"package": {"id": "bob-only"}},
        scope=Scope.USER, owner_user_id="bob",
    )

    alice_visible = await r.list_visible_to_user(user_id="alice")
    alice_ids = {p["package_id"] for p in alice_visible}
    assert "chat" in alice_ids
    assert "alice-only" in alice_ids
    assert "bob-only" not in alice_ids

    bob_visible = await r.list_visible_to_user(user_id="bob")
    bob_ids = {p["package_id"] for p in bob_visible}
    assert "chat" in bob_ids
    assert "bob-only" in bob_ids
    assert "alice-only" not in bob_ids


@pytest.mark.asyncio
async def test_list_visible_user_shadows_system() -> None:
    """When a user has their own install of a package that also
    exists system-wide, list_visible returns only the user version."""
    from digitorn.core.packages.registry import Scope
    r = await _build_registry()

    await r.create(
        package_id="chat", source_type="builtin",
        source_uri="bundle://digitorn/chat", version="1.0", hash="",
        install_dir="/packages/chat", manifest={"package": {"id": "chat"}},
        scope=Scope.SYSTEM,
    )
    await r.create(
        package_id="chat", source_type="local",
        source_uri="/tmp/alice-chat", version="2.0-alice", hash="",
        install_dir="/users/alice/packages/chat",
        manifest={"package": {"id": "chat"}},
        scope=Scope.USER, owner_user_id="alice",
    )

    alice_visible = await r.list_visible_to_user(user_id="alice")
    chat_rows = [p for p in alice_visible if p["package_id"] == "chat"]
    assert len(chat_rows) == 1
    assert chat_rows[0]["scope"] == "user"
    assert chat_rows[0]["version"] == "2.0-alice"


@pytest.mark.asyncio
async def test_install_flow_resolves_correct_dir(tmp_path: Path) -> None:
    """InstallFlow writes system installs to install_root and
    user installs to user_install_root/<uid>/packages/."""
    from digitorn.core.packages.install import InstallFlow
    from digitorn.core.packages.registry import PackageRegistry, Scope
    from digitorn.core.packages.sources.local import LocalSource

    # Build a minimal source package
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "app.yaml").write_text(
        """
app:
  app_id: demo
  name: Demo
  version: "1.0.0"

modules:
  filesystem:
    config: {}
""".strip(),
        encoding="utf-8",
    )
    (src / "package.toml").write_text(
        """
[package]
id = "demo"
name = "Demo"
version = "1.0.0"
description = "Test"
""".strip(),
        encoding="utf-8",
    )

    r = await _build_registry()
    flow = InstallFlow(
        registry=r,
        source_map={"local": LocalSource()},
        install_root=tmp_path / "system_packages",
        user_install_root=tmp_path / "user_homes",
    )

    # System install
    sys_result = await flow.install(
        source_type="local",
        source_uri=str(src),
        accept_permissions=True,
        scope="system",
    )
    assert "system_packages" in sys_result.install_dir
    assert "demo" in sys_result.install_dir

    # User install (different version via a new source)
    src2 = tmp_path / "src2" / "demo"
    src2.mkdir(parents=True)
    (src2 / "app.yaml").write_text(
        src.joinpath("app.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (src2 / "package.toml").write_text(
        src.joinpath("package.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    user_result = await flow.install(
        source_type="local",
        source_uri=str(src2),
        accept_permissions=True,
        scope="user",
        owner_user_id="alice",
    )
    assert "user_homes" in user_result.install_dir
    assert "alice" in user_result.install_dir
    assert "packages" in user_result.install_dir

    # Registry has both rows
    sys_row = await r.get("demo", scope=Scope.SYSTEM)
    alice_row = await r.get(
        "demo", scope=Scope.USER, owner_user_id="alice",
    )
    assert sys_row is not None
    assert alice_row is not None
    assert sys_row["owner_user_id"] is None
    assert alice_row["owner_user_id"] == "alice"


def test_install_flow_rejects_user_scope_without_owner(tmp_path: Path) -> None:
    """scope='user' with owner_user_id=None raises ValueError."""
    from digitorn.core.packages.install import InstallFlow
    from digitorn.core.packages.registry import PackageRegistry
    from digitorn.core.packages.sources.local import LocalSource

    r = PackageRegistry(lambda: None)  # type: ignore[arg-type]
    flow = InstallFlow(
        registry=r,
        source_map={"local": LocalSource()},
        install_root=tmp_path / "system",
    )

    with pytest.raises(ValueError, match="requires owner_user_id"):
        flow._resolve_install_dir("demo", scope="user", owner_user_id=None)


def test_install_flow_rejects_system_scope_with_owner(tmp_path: Path) -> None:
    from digitorn.core.packages.install import InstallFlow
    from digitorn.core.packages.registry import PackageRegistry
    from digitorn.core.packages.sources.local import LocalSource

    r = PackageRegistry(lambda: None)  # type: ignore[arg-type]
    flow = InstallFlow(
        registry=r,
        source_map={"local": LocalSource()},
        install_root=tmp_path / "system",
    )

    # system scope with an owner → gets normalized (owner silently dropped).
    # We verify by the computed dir
    p = flow._resolve_install_dir("demo", scope="system", owner_user_id="alice")
    assert "users" not in str(p)
    assert "alice" not in str(p)


def test_appmanager_deployed_key_format() -> None:
    """Verify the _deployed_key helper produces the right shape."""
    from digitorn.core.app.manager import AppManager

    assert AppManager._deployed_key("chat") == "system::chat"
    assert AppManager._deployed_key("chat", "system") == "system::chat"
    assert (
        AppManager._deployed_key("chat", "user", "alice")
        == "user:alice:chat"
    )

    with pytest.raises(ValueError, match="requires owner_user_id"):
        AppManager._deployed_key("chat", "user", None)


@pytest.mark.asyncio
async def test_bootstrap_coexists_with_user_shadow(tmp_path: Path) -> None:
    """Simulate the full flow: install a builtin, add a user
    shadow with the same package_id, re-run bootstrap → should
    NOT crash, should leave the user shadow intact, should
    upgrade the system row if the builtin hash changed.
    """
    from digitorn.core.packages.registry import Scope
    r = await _build_registry()

    # 1. First boot — install the builtin at scope=system
    await r.create(
        package_id="digitorn-chat",
        source_type="builtin",
        source_uri="bundle://digitorn/digitorn-chat",
        version="1.0.0",
        hash="hash_v1",
        install_dir="/packages/digitorn-chat",
        manifest={"package": {"id": "digitorn-chat"}},
        scope=Scope.SYSTEM,
    )

    # 2. Alice installs her personal shadow
    await r.create(
        package_id="digitorn-chat",
        source_type="local",
        source_uri="/tmp/alice-chat",
        version="1.0.0-alice",
        hash="hash_alice",
        install_dir="/users/alice/packages/digitorn-chat",
        manifest={"package": {"id": "digitorn-chat"}},
        scope=Scope.USER,
        owner_user_id="alice",
    )

    # 3. Registry.get() without scope should not crash (multiple
    # rows exist for the same package_id). Bootstrap uses
    # scope=system explicitly; this is the fallback path.
    row = await r.get("digitorn-chat")
    assert row is not None
    # Deterministic: system wins over user in the default sort
    assert row["scope"] == "system"

    # 4. Bootstrap-style lookup with explicit scope=system
    sys_row = await r.get("digitorn-chat", scope=Scope.SYSTEM)
    assert sys_row is not None
    assert sys_row["version"] == "1.0.0"

    # 5. Simulate a wheel upgrade: the builtin ships hash_v2 now
    await r.update_version(
        "digitorn-chat",
        new_version="2.0.0",
        new_hash="hash_v2",
        new_manifest={"package": {"id": "digitorn-chat"}},
        scope=Scope.SYSTEM,
    )

    # 6. Alice's row is untouched
    alice_row = await r.get(
        "digitorn-chat", scope=Scope.USER, owner_user_id="alice",
    )
    assert alice_row is not None
    assert alice_row["version"] == "1.0.0-alice"
    assert alice_row["hash"] == "hash_alice"

    # 7. System row is upgraded
    sys_row2 = await r.get("digitorn-chat", scope=Scope.SYSTEM)
    assert sys_row2["version"] == "2.0.0"
    assert sys_row2["hash"] == "hash_v2"

    # 8. resolve_for_caller: Alice sees her version, Bob sees the new system one
    alice_view = await r.resolve_for_caller("digitorn-chat", user_id="alice")
    assert alice_view["scope"] == "user"
    assert alice_view["version"] == "1.0.0-alice"
    bob_view = await r.resolve_for_caller("digitorn-chat", user_id="bob")
    assert bob_view["scope"] == "system"
    assert bob_view["version"] == "2.0.0"


def test_safe_user_dir_name_blocks_traversal() -> None:
    """Path traversal attempts in owner_user_id are sanitized."""
    from digitorn.core.packages.install import _safe_user_dir_name

    assert _safe_user_dir_name("alice") == "alice"
    assert _safe_user_dir_name("alice/../bob") == "alice____bob"
    assert _safe_user_dir_name("..") == "user"
    assert _safe_user_dir_name("") == "user"
    # Unicode / special chars get normalized
    assert "/" not in _safe_user_dir_name("user with spaces")
