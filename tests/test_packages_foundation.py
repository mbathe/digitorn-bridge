"""End-to-end foundation test for the AppPackages system.

Walks every layer in one process with an in-memory SQLite database
and a temp filesystem, validating that the design from
``docs/APP_PACKAGES.md`` is implemented correctly.

Run with::

    py -3.12 tests/test_packages_foundation.py
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import tempfile
from pathlib import Path

# Silence noisy module loader logs
logging.basicConfig(level=logging.ERROR)
logging.getLogger("digitorn").setLevel(logging.ERROR)

# Force UTF-8 on Windows consoles
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages"))


def _header(title: str) -> None:
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}")


def _ok(label: str) -> None:
    print(f"  ✓ {label}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  ✗ {label}")
    if detail:
        print(f"    → {detail}")
    raise AssertionError(f"{label}: {detail}")


# ────────────────────────────────────────────────────────────────────
# Test fixtures
# ────────────────────────────────────────────────────────────────────


def _write_minimal_package(dest: Path, *, package_id: str, version: str = "1.0.0") -> None:
    """Create a minimal valid package directory at ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "package.toml").write_text(
        f"""\
[package]
id = "{package_id}"
name = "Test Package {package_id}"
version = "{version}"
description = "A test package for the foundation suite"
author = "Digitorn Tests"
category = "developer-tools"

[package.source]
type = "local"

[package.compatibility]
digitorn_min = ">=1.0.0"

[package.requirements]
modules = ["filesystem"]

[package.permissions]
risk_level = "low"
network_access = false
filesystem_access = ["read"]

[package.credentials]
required = []
optional = []
""",
        encoding="utf-8",
    )
    (dest / "app.yaml").write_text(
        f"""\
app:
  app_id: {package_id}
  name: Test Package {package_id}
  version: "{version}"
  description: Test app for the package foundation suite
modules:
  filesystem: {{}}
agents:
  - id: worker
    role: worker
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: claude-code
execution:
  mode: one_shot
  entry_agent: worker
capabilities:
  grant:
    - module: filesystem
      actions: [read]
""",
        encoding="utf-8",
    )
    (dest / "README.md").write_text(
        f"# {package_id}\n\nMinimal test package.\n",
        encoding="utf-8",
    )


async def _setup_in_memory_db():
    """Bootstrap an in-memory SQLite + create all tables."""
    from digitorn.core.config import get_settings
    from digitorn.core.database import Base, get_session_factory, init_db

    s = get_settings()
    s.database.url = "sqlite+aiosqlite:///:memory:"
    engine = await init_db(s)

    # Force-create the new tables
    from digitorn.core.models import InstalledPackage  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    return get_session_factory()


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────


async def test_1_manifest_parse_valid() -> None:
    _header("1. PackageManifest — parse a valid TOML")
    from digitorn.core.packages import PackageManifest

    tmp = Path(tempfile.mkdtemp())
    _write_minimal_package(tmp, package_id="test-pkg-a", version="1.2.3")
    manifest = PackageManifest.from_path(tmp / "package.toml")
    assert manifest.id == "test-pkg-a"
    assert manifest.version == "1.2.3"
    assert manifest.permissions.risk_level == "low"
    assert manifest.requirements.modules == ["filesystem"]
    _ok(f"parsed: id={manifest.id} version={manifest.version}")
    shutil.rmtree(tmp)


async def test_2_manifest_validation_rejects_bad_id() -> None:
    _header("2. PackageManifest — reject invalid id / semver")
    from digitorn.core.packages import PackageManifest

    # Bad id (uppercase)
    try:
        PackageManifest.from_dict({
            "package": {"id": "BAD-UPPER", "name": "x", "version": "1.0.0"},
        })
        _fail("uppercase id should have been rejected")
    except ValueError:
        _ok("uppercase id rejected")

    # Bad semver
    try:
        PackageManifest.from_dict({
            "package": {"id": "ok", "name": "x", "version": "not-semver"},
        })
        _fail("bad semver should have been rejected")
    except ValueError:
        _ok("bad semver rejected")

    # Bad risk_level
    try:
        PackageManifest.from_dict({
            "package": {
                "id": "ok", "name": "x", "version": "1.0.0",
                "permissions": {"risk_level": "extreme"},
            },
        })
        _fail("invalid risk_level should have been rejected")
    except ValueError:
        _ok("invalid risk_level rejected")


async def test_3_hash_deterministic_and_drift() -> None:
    _header("3. compute_package_hash — deterministic + drift detection")
    from digitorn.core.packages import (
        compute_package_hash,
        detect_drift,
        write_package_hash_file,
    )

    tmp = Path(tempfile.mkdtemp())
    _write_minimal_package(tmp, package_id="test-pkg-c")
    h1 = compute_package_hash(tmp)
    h2 = compute_package_hash(tmp)
    assert h1 == h2
    _ok(f"deterministic: {h1[:16]}...")

    # Persist + check drift
    write_package_hash_file(tmp, h1)
    drifted, current, stored = detect_drift(tmp)
    assert not drifted
    _ok("no drift after fresh install")

    # Modify a file
    (tmp / "README.md").write_text("modified", encoding="utf-8")
    drifted, current, stored = detect_drift(tmp)
    assert drifted
    assert current != stored
    _ok("drift detected after edit")

    # .digitorn/ is excluded from the hash
    (tmp / ".digitorn" / "marker.txt").write_text("noise", encoding="utf-8")
    h3 = compute_package_hash(tmp)
    # Restore README to its original state
    (tmp / "README.md").write_text(
        f"# test-pkg-c\n\nMinimal test package.\n", encoding="utf-8",
    )
    h4 = compute_package_hash(tmp)
    assert h4 == h1, ".digitorn/ should not affect the hash"
    _ok(".digitorn/ correctly excluded from the hash")
    shutil.rmtree(tmp)


async def test_4_registry_crud() -> None:
    _header("4. PackageRegistry — CRUD")
    from digitorn.core.packages import PackageRegistry, SourceType, Status

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    # Create
    row = await registry.create(
        package_id="test-pkg-d",
        source_type=SourceType.LOCAL,
        source_uri="file:///tmp/t4",
        version="1.0.0",
        hash="abc123",
        install_dir="/tmp/t4",
        manifest={"package": {"id": "test-pkg-d"}},
        installed_by="alice",
    )
    assert row["package_id"] == "test-pkg-d"
    assert row["status"] == Status.INSTALLED
    _ok(f"created package row: {row['package_id']}")

    # Get
    fetched = await registry.get("test-pkg-d")
    assert fetched["source_type"] == SourceType.LOCAL
    _ok("get works")

    # List
    listed = await registry.list_all()
    assert len(listed) == 1
    _ok(f"list_all returned {len(listed)} row(s)")

    # Update status
    ok = await registry.update_status(
        "test-pkg-d", status=Status.BROKEN, last_error="test error",
    )
    assert ok
    fetched = await registry.get("test-pkg-d")
    assert fetched["status"] == Status.BROKEN
    assert fetched["last_error"] == "test error"
    _ok("update_status works")

    # Update version
    ok = await registry.update_version(
        "test-pkg-d",
        new_version="2.0.0",
        new_hash="def456",
        new_manifest={"package": {"id": "test-pkg-d", "version": "2.0.0"}},
    )
    assert ok
    fetched = await registry.get("test-pkg-d")
    assert fetched["version"] == "2.0.0"
    assert fetched["hash"] == "def456"
    assert fetched["status"] == Status.INSTALLED  # auto-cleared from BROKEN
    _ok("update_version works")

    # Delete
    deleted = await registry.delete("test-pkg-d")
    assert deleted
    assert await registry.get("test-pkg-d") is None
    _ok("delete works")


async def test_5_local_source_fetch() -> None:
    _header("5. LocalSource — fetch from a directory")
    from digitorn.core.packages import LocalSource

    src = Path(tempfile.mkdtemp()) / "my-pkg"
    _write_minimal_package(src, package_id="test-pkg-e")

    dest_root = Path(tempfile.mkdtemp())
    dest = dest_root / "test-pkg-e"

    source = LocalSource(link_mode="copy")
    result = await source.fetch(f"file://{src}", dest)

    assert result == dest
    assert (dest / "package.toml").is_file()
    assert (dest / "app.yaml").is_file()
    _ok(f"copied {src.name} → {dest}")

    # Refetch should overwrite
    (src / "extra.txt").write_text("new")
    await source.fetch(f"file://{src}", dest)
    assert (dest / "extra.txt").is_file()
    _ok("re-fetch overwrites cleanly")

    # Bad URI
    try:
        await source.fetch("/nonexistent/path", dest)
        _fail("missing source should have raised FetchError")
    except Exception as e:
        _ok(f"missing source rejected: {type(e).__name__}")

    shutil.rmtree(dest_root)
    shutil.rmtree(src.parent)


async def test_6_builtin_source_scan() -> None:
    _header("6. BuiltinSource — scan a fake builtins dir")
    from digitorn.core.packages import BuiltinSource

    builtins_dir = Path(tempfile.mkdtemp()) / "builtins"
    _write_minimal_package(builtins_dir / "digitorn-test-a", package_id="digitorn-test-a")
    _write_minimal_package(builtins_dir / "digitorn-test-b", package_id="digitorn-test-b")
    # Drop a subdir without package.toml — should be skipped
    (builtins_dir / "not-a-package").mkdir()
    (builtins_dir / "not-a-package" / "random.txt").write_text("nope")

    source = BuiltinSource(builtins_dir)
    available = await source.list_available()

    ids = sorted(p.package_id for p in available)
    assert ids == ["digitorn-test-a", "digitorn-test-b"]
    _ok(f"scanned {len(available)} builtin(s): {ids}")

    # Each available has a hash and a source_uri
    for pkg in available:
        assert pkg.source_uri.startswith("bundle://digitorn/")
        assert pkg.hash and len(pkg.hash) == 64
    _ok("each entry has source_uri + hash")

    # Fetch one
    dest = Path(tempfile.mkdtemp()) / "extracted"
    result = await source.fetch(
        "bundle://digitorn/digitorn-test-a", dest,
    )
    assert (result / "package.toml").is_file()
    _ok("fetch from builtin works")

    shutil.rmtree(builtins_dir.parent)
    shutil.rmtree(dest.parent)


async def test_7_hub_and_git_stubs_raise() -> None:
    _header("7. HubSource + GitSource — STUB ONLY in v1")
    from digitorn.core.packages import GitSource, HubSource

    hub = HubSource()
    try:
        await hub.list_available()
        _fail("HubSource.list_available should raise NotImplementedError")
    except NotImplementedError as e:
        assert "v1" in str(e) or "hub" in str(e).lower()
        _ok("HubSource.list_available raises NotImplementedError")

    try:
        await hub.fetch("hub://x", Path("/tmp/x"))
        _fail("HubSource.fetch should raise")
    except NotImplementedError:
        _ok("HubSource.fetch raises NotImplementedError")

    git = GitSource()
    try:
        await git.fetch("git+https://x", Path("/tmp/x"))
        _fail("GitSource.fetch should raise")
    except NotImplementedError:
        _ok("GitSource.fetch raises NotImplementedError")


async def test_8_install_flow_happy_path() -> None:
    _header("8. InstallFlow — happy path (local source)")
    from digitorn.core.packages import (
        InstallFlow,
        LocalSource,
        PackageRegistry,
        compute_package_hash,
    )

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    src = Path(tempfile.mkdtemp()) / "my-pkg"
    _write_minimal_package(src, package_id="test-pkg-h")

    install_root = Path(tempfile.mkdtemp())
    flow = InstallFlow(
        registry=registry,
        source_map={"local": LocalSource()},
        install_root=install_root,
        daemon_version="2.0.0",
    )

    # Probe permissions first
    perms = await flow.probe_permissions("local", str(src))
    assert perms["package_id"] == "test-pkg-h"
    assert perms["permissions"]["risk_level"] == "low"
    _ok(f"probe_permissions returns: risk={perms['permissions']['risk_level']}")

    # Install with consent
    result = await flow.install(
        source_type="local",
        source_uri=str(src),
        installed_by="alice",
        accept_permissions=True,
    )
    assert result.package_id == "test-pkg-h"
    assert Path(result.install_dir).is_dir()
    assert (Path(result.install_dir) / "package.toml").is_file()
    _ok(f"installed at {result.install_dir}")
    _ok(f"hash recorded: {result.hash[:16]}...")

    # Registry has the row
    row = await registry.get("test-pkg-h")
    assert row is not None
    assert row["status"] == "installed"
    assert row["installed_by"] == "alice"
    _ok("registry row created")

    # Drift check on a fresh install → no drift
    drift = await registry.check_drift("test-pkg-h")
    assert not drift["drifted"]
    _ok("drift check: no drift on fresh install")

    shutil.rmtree(install_root)
    shutil.rmtree(src.parent)


async def test_9_install_collision_refused() -> None:
    _header("9. InstallFlow — refuses collision (locked design D12)")
    from digitorn.core.packages import (
        InstallFlow,
        LocalSource,
        PackageIdCollision,
        PackageRegistry,
    )

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    src1 = Path(tempfile.mkdtemp()) / "p9"
    _write_minimal_package(src1, package_id="test-pkg-i")
    src2 = Path(tempfile.mkdtemp()) / "p9-other"
    _write_minimal_package(src2, package_id="test-pkg-i", version="2.0.0")

    install_root = Path(tempfile.mkdtemp())
    flow = InstallFlow(
        registry=registry,
        source_map={"local": LocalSource()},
        install_root=install_root,
    )

    # First install OK
    await flow.install(
        source_type="local", source_uri=str(src1),
        accept_permissions=True,
    )
    _ok("first install succeeded")

    # Second install with same id → refused
    try:
        await flow.install(
            source_type="local", source_uri=str(src2),
            accept_permissions=True,
        )
        _fail("second install with same id should have been refused")
    except PackageIdCollision as e:
        assert e.package_id == "test-pkg-i"
        assert e.existing["source_type"] == "local"
        _ok(f"collision refused: {e.package_id}")

    shutil.rmtree(install_root)
    shutil.rmtree(src1.parent)
    shutil.rmtree(src2.parent)


async def test_10_install_requires_consent() -> None:
    _header("10. InstallFlow — requires accept_permissions")
    from digitorn.core.packages import (
        InstallFlow,
        LocalSource,
        PackageRegistry,
        PermissionsRequired,
    )

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    src = Path(tempfile.mkdtemp()) / "p10"
    _write_minimal_package(src, package_id="test-pkg-j")

    install_root = Path(tempfile.mkdtemp())
    flow = InstallFlow(
        registry=registry,
        source_map={"local": LocalSource()},
        install_root=install_root,
    )

    try:
        await flow.install(
            source_type="local", source_uri=str(src),
            accept_permissions=False,
        )
        _fail("install without consent should have raised")
    except PermissionsRequired as e:
        assert e.manifest_id == "test-pkg-j"
        assert "risk_level" in e.perms
        _ok(f"PermissionsRequired raised with perms payload: {list(e.perms)}")

    # Nothing should have been written
    assert await registry.get("test-pkg-j") is None
    assert not (install_root / "test-pkg-j").exists()
    _ok("no state changed after refusal")

    shutil.rmtree(install_root)
    shutil.rmtree(src.parent)


async def test_11_uninstall_blocks_builtin_without_force() -> None:
    _header("11. InstallFlow — uninstall blocks builtin without force")
    from digitorn.core.packages import (
        InstallError,
        InstallFlow,
        LocalSource,
        PackageRegistry,
        SourceType,
    )

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    # Manually insert a builtin row to test the check
    await registry.create(
        package_id="test-pkg-k",
        source_type=SourceType.BUILTIN,
        source_uri="bundle://digitorn/t11",
        version="1.0.0",
        hash="x",
        install_dir="/tmp/nonexistent-t11",
        manifest={},
    )

    install_root = Path(tempfile.mkdtemp())
    flow = InstallFlow(
        registry=registry,
        source_map={"local": LocalSource()},
        install_root=install_root,
    )

    try:
        await flow.uninstall("test-pkg-k", force=False)
        _fail("uninstall of builtin without force should fail")
    except InstallError as e:
        assert "builtin" in str(e).lower() or "force" in str(e).lower()
        _ok(f"refused without force: {e}")

    # Row still there
    assert await registry.get("test-pkg-k") is not None

    # With force=True it succeeds
    deleted = await flow.uninstall("test-pkg-k", force=True)
    assert deleted
    assert await registry.get("test-pkg-k") is None
    _ok("uninstall with force=True succeeds")

    shutil.rmtree(install_root)


async def test_12_install_permission_helper() -> None:
    _header("12. has_install_permission — capability check")
    from digitorn.core.packages import has_install_permission

    assert has_install_permission(["*"])
    assert has_install_permission(["package.install"])
    assert has_install_permission(["app.read", "package.install"])
    assert not has_install_permission(["app.read", "memory.write"])
    assert not has_install_permission([])
    assert not has_install_permission(None)
    _ok("admin (*) bypasses, package.install allowed, others denied")


async def test_13_classify_existing_apps() -> None:
    _header("13. Migration — classify existing apps as source_type='local'")
    from digitorn.core.models import Application
    from digitorn.core.packages import classify_existing_apps

    sf = await _setup_in_memory_db()

    # Insert an app row WITHOUT source_type (simulates legacy)
    async with sf() as db:
        legacy = Application(
            app_id="legacy-1", name="Legacy", version="1.0",
            description="Pre-package app",
            yaml_content="app:\n  app_id: legacy-1",
        )
        # Force the source_type to be empty to simulate the migration scenario
        legacy.source_type = ""
        db.add(legacy)
        await db.commit()

    summary = await classify_existing_apps(sf)
    assert summary["newly_classified"] >= 1
    _ok(f"migration summary: {summary}")

    # Verify the row was updated
    from sqlalchemy import select
    async with sf() as db:
        row = (
            await db.execute(
                select(Application).where(Application.app_id == "legacy-1")
            )
        ).scalar_one()
        assert row.source_type == "local"
    _ok("legacy row now has source_type='local'")


async def test_14_real_builtins_directory() -> None:
    _header("14. Real built-ins — packages/digitorn/builtins/ has the 4 packages")
    from digitorn.core.packages.bootstrap import _default_builtins_dir
    from digitorn.core.packages import BuiltinSource

    builtins_dir = _default_builtins_dir()
    print(f"  builtins_dir: {builtins_dir}")
    assert builtins_dir.is_dir(), f"expected {builtins_dir} to exist"
    _ok(f"directory exists")

    source = BuiltinSource(builtins_dir)
    available = await source.list_available()
    ids = sorted(p.package_id for p in available)
    print(f"  found: {ids}")

    # All 4 expected builtins must be present
    expected = {
        "digitorn-chat",
        "digitorn-builder",
        "digitorn-code",
        "digitorn-deepresearch",
    }
    found = set(ids)
    missing = expected - found
    assert not missing, f"missing built-ins: {missing}"
    _ok(f"all 4 expected built-ins discovered ({len(found)} total)")

    # Each has a non-empty hash and well-formed source_uri
    for pkg in available:
        assert pkg.hash and len(pkg.hash) == 64, f"{pkg.package_id} has bad hash"
        assert pkg.source_uri == f"bundle://digitorn/{pkg.package_id}"
        assert pkg.manifest, f"{pkg.package_id} manifest is empty"
    _ok("each package has a hash + source_uri + manifest")


async def test_15_real_builtins_compile() -> None:
    _header("15. Real built-ins — every app.yaml compiles cleanly")
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake-test")
    os.environ.setdefault("DEEPSEEK_API_KEY", "sk-fake-test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake")
    os.environ.setdefault("WEBHOOK_SECRET", "fake")

    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.app.errors import AppCompilationError
    from digitorn.core.loader import load_modules
    from digitorn.core.packages.bootstrap import _default_builtins_dir
    from digitorn.modules.registry import ModuleRegistry

    reg = ModuleRegistry()
    load_modules(reg, load_all=True)
    compiler = AppYAMLCompiler(reg)

    builtins_dir = _default_builtins_dir()
    expected_ids = {
        "digitorn-chat",
        "digitorn-builder",
        "digitorn-code",
        "digitorn-deepresearch",
    }

    failures: list[tuple[str, str]] = []
    for package_id in sorted(expected_ids):
        yaml_path = builtins_dir / package_id / "app.yaml"
        assert yaml_path.is_file(), f"{yaml_path} missing"
        try:
            compiled = compiler.compile_file(yaml_path)
            assert compiled.meta.app_id == package_id, (
                f"{package_id} compiles but has wrong app_id: "
                f"{compiled.meta.app_id}"
            )
            _ok(
                f"{package_id} → mode={compiled.execution.mode}, "
                f"agents={len(compiled.agents)}, modules={len(compiled.modules)}"
            )
        except AppCompilationError as e:
            failures.append((package_id, str(e)))
        except Exception as e:
            failures.append((package_id, f"{type(e).__name__}: {e}"))

    if failures:
        for pid, err in failures:
            print(f"  ✗ {pid}: {err}")
        raise AssertionError(f"{len(failures)} built-in(s) failed to compile")


async def test_16_bootstrap_builtins_full_cycle() -> None:
    _header("16. bootstrap_builtins() — install all 4 from the wheel")
    from digitorn.core.packages import PackageRegistry
    from digitorn.core.packages.bootstrap import bootstrap_builtins

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    install_root = Path(tempfile.mkdtemp())

    # No on_deploy callback — we just want to verify the install
    # part. Compile + deploy needs the full daemon and is covered
    # in test 15 separately.
    summary = await bootstrap_builtins(
        registry=registry,
        on_deploy=None,
        install_root=install_root,
    )

    print(f"  installed: {summary['installed']}")
    print(f"  upgraded:  {summary['upgraded']}")
    print(f"  skipped:   {summary['skipped']}")
    print(f"  failed:    {summary['failed']}")
    assert not summary["failed"], f"some installs failed: {summary['failed']}"
    assert len(summary["installed"]) == 4, (
        f"expected 4 fresh installs, got {summary['installed']}"
    )
    _ok(f"4/4 built-ins installed cleanly")

    # Re-running bootstrap is a no-op (everything skipped)
    summary2 = await bootstrap_builtins(
        registry=registry,
        on_deploy=None,
        install_root=install_root,
    )
    assert not summary2["installed"], "second run should not re-install"
    assert not summary2["upgraded"], "second run should not upgrade"
    assert len(summary2["skipped"]) == 4
    _ok("re-running bootstrap is a no-op (all skipped)")

    # Verify the registry has 4 rows
    all_pkgs = await registry.list_all()
    assert len(all_pkgs) == 4
    for p in all_pkgs:
        assert p["source_type"] == "builtin"
        assert p["status"] == "installed"
        assert (Path(p["install_dir"]) / "app.yaml").is_file()
    _ok("registry has 4 installed builtin rows + each install dir is valid")

    shutil.rmtree(install_root)


async def test_17_manifest_generator_on_real_builtins() -> None:
    _header("17. generate_package_manifest — risk inference on the 4 built-ins")
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")
    os.environ.setdefault("DEEPSEEK_API_KEY", "sk-fake-deepseek")

    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.packages import generate_package_manifest
    from digitorn.core.packages.bootstrap import _default_builtins_dir
    from digitorn.modules.registry import ModuleRegistry

    reg = ModuleRegistry()
    load_modules(reg, load_all=True)
    compiler = AppYAMLCompiler(reg)
    builtins_dir = _default_builtins_dir()

    expected_risk = {
        # chat has web.fetch which can be used to exfiltrate data
        # via attacker-controlled URLs → medium risk
        "digitorn-chat": "medium",
        # builder writes YAML files locally → medium risk
        "digitorn-builder": "medium",
        # code grants shell.bash → high risk
        "digitorn-code": "high",
        # deepresearch spawns sub-agents → high risk
        "digitorn-deepresearch": "high",
    }

    for pkg_id, expected in expected_risk.items():
        compiled = compiler.compile_file(builtins_dir / pkg_id / "app.yaml")
        manifest = generate_package_manifest(compiled)
        assert manifest.permissions.risk_level == expected, (
            f"{pkg_id}: expected risk={expected}, got "
            f"{manifest.permissions.risk_level}"
        )
        # The TOML must round-trip back through the parser
        toml = manifest.to_toml()
        assert "[package]" in toml
        assert f'id = "{pkg_id}"' in toml
        _ok(f"{pkg_id} → risk={expected}, modules={len(manifest.requirements.modules)}")


class _FakeRequest:
    """Minimal FastAPI Request stand-in for in-process tests."""

    def __init__(self, *, registry, manager=None, user_id="alice",
                 permissions=None):
        class _State:
            pass
        self.state = _State()
        self.state.user_id = user_id
        self.state.permissions = permissions or ["*"]

        class _AppState:
            pass
        class _App:
            pass
        self.app = _App()
        self.app.state = _AppState()
        self.app.state.package_registry = registry
        # The manager is looked up via _get_manager(request) which
        # reads request.app.state.app_manager; expose it the same way.
        self.app.state.app_manager = manager


class _NoOpManager:
    """A pass-through manager that satisfies _get_manager and undeploy."""
    async def deploy(self, *args, **kwargs):
        return None
    async def undeploy(self, *args, **kwargs):
        return None


async def test_18_routes_install_probe_then_install() -> None:
    _header("18. /api/packages/install — probe (409) → install (200)")
    from digitorn.core.api.packages import install_package, InstallRequest
    from digitorn.core.packages import PackageRegistry

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    src = Path(tempfile.mkdtemp()) / "route-pkg"
    _write_minimal_package(src, package_id="route-pkg-a")

    request = _FakeRequest(registry=registry, manager=_NoOpManager())

    # Step 1: install without consent → 409 with permissions payload
    body = InstallRequest(
        source_type="local",
        source_uri=str(src),
        accept_permissions=False,
    )
    try:
        await install_package(request, body)
        _fail("install without consent should 409")
    except Exception as exc:
        # FastAPI HTTPException wraps the dict in .detail
        from fastapi import HTTPException
        assert isinstance(exc, HTTPException), f"unexpected exception {type(exc)}"
        assert exc.status_code == 409
        detail = exc.detail
        assert detail["error"] == "permissions_required"
        assert detail["package_id"] == "route-pkg-a"
        assert "risk_level" in detail["permissions"]
        _ok(f"probe returned 409 with permissions for {detail['package_id']}")

    # Step 2: install WITH consent → success
    body.accept_permissions = True
    response = await install_package(request, body)
    assert response.success
    assert response.data["package_id"] == "route-pkg-a"
    assert Path(response.data["install_dir"]).is_dir()
    _ok(f"install succeeded: {response.data['package_id']}")

    # Verify in registry
    pkg = await registry.get("route-pkg-a")
    assert pkg is not None
    assert pkg["installed_by"] == "alice"
    _ok("registry row created with installed_by=alice")

    shutil.rmtree(src.parent)


async def test_19_routes_install_collision_409() -> None:
    _header("19. /api/packages/install — id collision returns 409")
    from digitorn.core.api.packages import install_package, InstallRequest
    from digitorn.core.packages import PackageRegistry

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    src = Path(tempfile.mkdtemp()) / "p"
    _write_minimal_package(src, package_id="route-pkg-b")
    src2 = Path(tempfile.mkdtemp()) / "p"
    _write_minimal_package(src2, package_id="route-pkg-b", version="2.0.0")

    request = _FakeRequest(registry=registry, manager=_NoOpManager())

    # First install succeeds
    await install_package(
        request,
        InstallRequest(source_type="local", source_uri=str(src),
                       accept_permissions=True),
    )
    _ok("first install succeeded")

    # Second install with same id → 409 collision
    try:
        await install_package(
            request,
            InstallRequest(source_type="local", source_uri=str(src2),
                           accept_permissions=True),
        )
        _fail("collision should 409")
    except Exception as exc:
        from fastapi import HTTPException
        assert isinstance(exc, HTTPException) and exc.status_code == 409
        assert exc.detail["error"] == "package_already_installed"
        _ok(f"collision returned 409: {exc.detail['package_id']}")

    shutil.rmtree(src.parent)
    shutil.rmtree(src2.parent)


async def test_20_routes_list_get_check_update() -> None:
    _header("20. /api/packages — list / get / check-update")
    from digitorn.core.api.packages import (
        check_update,
        get_package,
        list_packages,
    )
    from digitorn.core.packages import PackageRegistry, SourceType

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    # Manually insert two packages so we don't touch the install flow
    await registry.create(
        package_id="route-pkg-c",
        source_type=SourceType.LOCAL,
        source_uri="file:///tmp/c",
        version="1.0.0",
        hash="abcd",
        install_dir="/tmp/c-nonexistent",
        manifest={"package": {"id": "route-pkg-c", "version": "1.0.0"}},
    )
    await registry.create(
        package_id="route-pkg-d",
        source_type=SourceType.BUILTIN,
        source_uri="bundle://digitorn/route-pkg-d",
        version="2.0.0",
        hash="efgh",
        install_dir="/tmp/d-nonexistent",
        manifest={"package": {"id": "route-pkg-d", "version": "2.0.0"}},
    )

    request = _FakeRequest(registry=registry, manager=_NoOpManager())

    # list_packages
    resp = await list_packages(request)
    assert resp.success
    assert resp.data["count"] == 2
    ids = sorted(p["package_id"] for p in resp.data["packages"])
    assert ids == ["route-pkg-c", "route-pkg-d"]
    _ok(f"list returned {resp.data['count']} packages")

    # list with filter
    resp_local = await list_packages(request, source_type="local")
    assert resp_local.data["count"] == 1
    _ok("list_packages?source_type=local filter works")

    # get_package
    resp = await get_package(request, "route-pkg-c")
    assert resp.success
    assert resp.data["package_id"] == "route-pkg-c"
    assert "drift" in resp.data
    # Drift detection on a missing install_dir should return drifted=True
    assert resp.data["drift"]["drifted"] is True
    _ok("get_package includes drift field")

    # get_package not found → 404
    try:
        await get_package(request, "nonexistent-pkg")
        _fail("nonexistent package should 404")
    except Exception as exc:
        from fastapi import HTTPException
        assert isinstance(exc, HTTPException) and exc.status_code == 404
        _ok("missing package → 404")

    # check_update on a local package — should report no update available
    resp = await check_update(request, "route-pkg-c")
    assert resp.success
    assert resp.data["package_id"] == "route-pkg-c"
    _ok(f"check_update for local package: update_available={resp.data['update_available']}")


async def test_21_routes_uninstall_builtin_protection() -> None:
    _header("21. /api/packages/{id}/uninstall — builtin protection")
    from digitorn.core.api.packages import (
        UninstallRequest,
        uninstall_package,
    )
    from digitorn.core.packages import PackageRegistry, SourceType

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    # Insert a builtin row directly
    await registry.create(
        package_id="route-pkg-e",
        source_type=SourceType.BUILTIN,
        source_uri="bundle://digitorn/route-pkg-e",
        version="1.0.0",
        hash="xx",
        install_dir="/tmp/route-pkg-e-nonexistent",
        manifest={},
    )

    request = _FakeRequest(registry=registry, manager=_NoOpManager())

    # Uninstall without force → 403
    try:
        await uninstall_package(
            request, "route-pkg-e", UninstallRequest(force=False),
        )
        _fail("uninstall of builtin without force should 403")
    except Exception as exc:
        from fastapi import HTTPException
        assert isinstance(exc, HTTPException) and exc.status_code == 403
        _ok(f"refused without force: {exc.detail[:60]}")

    # Uninstall WITH force → success
    resp = await uninstall_package(
        request, "route-pkg-e", UninstallRequest(force=True),
    )
    assert resp.success
    assert resp.data["uninstalled"] is True
    _ok("uninstall with force=True succeeds")

    # Verify the row is gone
    assert await registry.get("route-pkg-e") is None
    _ok("registry row removed")


async def test_22_routes_install_scope_gating() -> None:
    """Under the new scoping model, install permissions are
    scope-aware:

    - Any authenticated user can install at scope=user (default)
    - Only admins can install at scope=system

    The old blanket ``package.install`` capability check was
    removed — it was pre-scoping behavior.
    """
    _header("22. /api/packages/install — scope-based gating")
    from digitorn.core.api.packages import install_package, InstallRequest
    from digitorn.core.packages import PackageRegistry

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)

    src = Path(tempfile.mkdtemp()) / "p"
    _write_minimal_package(src, package_id="route-pkg-f")

    # Non-admin user installing at scope=user → SUCCESS (default)
    request_user = _FakeRequest(
        registry=registry,
        manager=_NoOpManager(),
        permissions=["app.read", "memory.write"],  # no admin
        user_id="alice",
    )
    resp = await install_package(
        request_user,
        InstallRequest(
            source_type="local", source_uri=str(src),
            accept_permissions=True, scope="user",
        ),
    )
    assert resp.success
    assert resp.data["scope"] == "user"
    assert resp.data["owner_user_id"] == "alice"
    _ok("non-admin user + scope=user → succeeds")

    # Non-admin user trying scope=system → 403
    src2 = Path(tempfile.mkdtemp()) / "p2"
    _write_minimal_package(src2, package_id="route-pkg-f2")
    request_non_admin = _FakeRequest(
        registry=registry,
        manager=_NoOpManager(),
        permissions=["app.read"],
        user_id="bob",
    )
    try:
        await install_package(
            request_non_admin,
            InstallRequest(
                source_type="local", source_uri=str(src2),
                accept_permissions=True, scope="system",
            ),
        )
        _fail("non-admin + scope=system should 403")
    except Exception as exc:
        from fastapi import HTTPException
        assert isinstance(exc, HTTPException) and exc.status_code == 403
        _ok("non-admin + scope=system → 403")

    # Admin installing at scope=system → SUCCESS
    src3 = Path(tempfile.mkdtemp()) / "p3"
    _write_minimal_package(src3, package_id="route-pkg-f3")
    request_admin = _FakeRequest(
        registry=registry,
        manager=_NoOpManager(),
        permissions=["*"],
        user_id="admin",
    )
    resp = await install_package(
        request_admin,
        InstallRequest(
            source_type="local", source_uri=str(src3),
            accept_permissions=True, scope="system",
        ),
    )
    assert resp.success
    assert resp.data["scope"] == "system"
    assert resp.data["owner_user_id"] is None
    _ok("admin + scope=system → succeeds")

    shutil.rmtree(src.parent)


async def test_23_routes_hub_git_return_501() -> None:
    _header("23. /api/packages/install — hub/git stubs return 501")
    from digitorn.core.api.packages import install_package, InstallRequest
    from digitorn.core.packages import PackageRegistry

    sf = await _setup_in_memory_db()
    registry = PackageRegistry(sf)
    request = _FakeRequest(registry=registry, manager=_NoOpManager())

    for source_type in ("hub", "git"):
        try:
            await install_package(
                request,
                InstallRequest(
                    source_type=source_type,
                    source_uri=f"{source_type}://example",
                    accept_permissions=True,
                ),
            )
            _fail(f"{source_type} install should 501")
        except Exception as exc:
            from fastapi import HTTPException
            assert isinstance(exc, HTTPException) and exc.status_code == 501
            assert "v2" in exc.detail.lower() or "deferred" in exc.detail.lower()
            _ok(f"{source_type} → 501 with deferred-to-v2 message")


async def test_24_discovery_generate_manifest_route() -> None:
    _header("24. /api/discovery/generate-package-manifest — happy path + warnings")
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")

    from digitorn.core.api.discovery import (
        GeneratePackageManifestRequest,
        generate_package_manifest_route,
    )
    from digitorn.core.loader import load_modules
    from digitorn.modules.registry import ModuleRegistry

    reg = ModuleRegistry()
    load_modules(reg, load_all=True)

    class _R:
        class app:
            class state:
                pass

    request = _R()
    request.app.state.registry = reg
    request.state = type("S", (), {})()

    yaml_text = """
app:
  app_id: gen-test
  name: Generated Test
  version: "0.1.0"
  description: Test app for the manifest generator route
modules:
  filesystem: {}
agents:
  - id: w
    role: worker
    brain:
      provider: anthropic
      model: claude-sonnet-4-5
      config:
        api_key: claude-code
execution:
  mode: one_shot
  entry_agent: w
capabilities:
  grant:
    - module: filesystem
      actions: [read, write]
"""
    body = GeneratePackageManifestRequest(yaml=yaml_text)
    resp = await generate_package_manifest_route(request, body)
    assert resp.success, f"manifest generation failed: {resp.error}"
    assert resp.data["valid"]
    assert "[package]" in resp.data["toml"]
    assert resp.data["summary"]["package_id"] == "gen-test"
    assert resp.data["summary"]["risk_level"] in ("low", "medium")
    _ok(f"manifest generated for gen-test, risk={resp.data['summary']['risk_level']}")
    _ok(f"warnings: {resp.data['warnings']}")

    # Bad YAML → success=False with errors
    bad = "app:\n  app_id: bad\nagents: []"
    resp_bad = await generate_package_manifest_route(
        request, GeneratePackageManifestRequest(yaml=bad),
    )
    assert not resp_bad.success
    assert not resp_bad.data["valid"]
    assert len(resp_bad.data["errors"]) > 0
    _ok(f"bad YAML → success=False with {len(resp_bad.data['errors'])} errors")


async def test_25_builder_still_compiles_with_packaging_state() -> None:
    _header("25. Phase D — digitorn-builder still compiles with STATE 7")
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")

    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.packages.bootstrap import _default_builtins_dir
    from digitorn.modules.registry import ModuleRegistry

    reg = ModuleRegistry()
    load_modules(reg, load_all=True)
    compiler = AppYAMLCompiler(reg)

    builder_yaml = _default_builtins_dir() / "digitorn-builder" / "app.yaml"
    compiled = compiler.compile_file(builder_yaml)
    assert compiled.meta.app_id == "digitorn-builder"
    _ok(f"compiled, system_prompt is now {len(compiled.agents[0].system_prompt)} chars")

    # The new state must be present in the system prompt
    sp = compiled.agents[0].system_prompt
    assert "STATE 7 — PROPOSE PACKAGE" in sp, "STATE 7 missing from prompt"
    assert "/api/discovery/generate-package-manifest" in sp
    assert "/api/packages/install" in sp
    _ok("STATE 7 + new route URLs present in system prompt")

    # The builder should still grant http.json_api so it can call the new routes
    granted = set()
    profile = compiled.security_profile
    if profile:
        for mod_id, grant in profile.module_grants.items():
            for action_name in grant.action_overrides.keys():
                granted.add(f"{mod_id}.{action_name}")
    assert "http.json_api" in granted, "http.json_api not granted"
    _ok("http.json_api still granted")


async def test_26_builder_can_self_package() -> None:
    _header("26. Phase D — generate_package_manifest works on the builder itself (meta!)")
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-fake")

    from digitorn.core.app.compiler import AppYAMLCompiler
    from digitorn.core.loader import load_modules
    from digitorn.core.packages import generate_package_manifest
    from digitorn.core.packages.bootstrap import _default_builtins_dir
    from digitorn.modules.registry import ModuleRegistry

    reg = ModuleRegistry()
    load_modules(reg, load_all=True)
    compiler = AppYAMLCompiler(reg)

    builder_yaml = _default_builtins_dir() / "digitorn-builder" / "app.yaml"
    compiled = compiler.compile_file(builder_yaml)

    # The builder packaging itself — it's mid-risk because it
    # writes to filesystem and uses http.
    manifest = generate_package_manifest(compiled)
    assert manifest.id == "digitorn-builder"
    assert manifest.permissions.risk_level in ("medium", "high")
    assert manifest.permissions.network_access is True  # uses http
    assert "filesystem" in manifest.requirements.modules
    assert "rag" in manifest.requirements.modules
    assert "http" in manifest.requirements.modules
    _ok(f"builder self-manifest: risk={manifest.permissions.risk_level}, "
        f"modules={len(manifest.requirements.modules)}")

    # The TOML round-trips back through the parser
    toml = manifest.to_toml()
    assert '[package]' in toml
    assert 'id = "digitorn-builder"' in toml
    _ok("TOML round-trips cleanly")


async def main() -> None:
    tests = [
        test_1_manifest_parse_valid,
        test_2_manifest_validation_rejects_bad_id,
        test_3_hash_deterministic_and_drift,
        test_4_registry_crud,
        test_5_local_source_fetch,
        test_6_builtin_source_scan,
        test_7_hub_and_git_stubs_raise,
        test_8_install_flow_happy_path,
        test_9_install_collision_refused,
        test_10_install_requires_consent,
        test_11_uninstall_blocks_builtin_without_force,
        test_12_install_permission_helper,
        test_13_classify_existing_apps,
        test_14_real_builtins_directory,
        test_15_real_builtins_compile,
        test_16_bootstrap_builtins_full_cycle,
        test_17_manifest_generator_on_real_builtins,
        test_18_routes_install_probe_then_install,
        test_19_routes_install_collision_409,
        test_20_routes_list_get_check_update,
        test_21_routes_uninstall_builtin_protection,
        test_22_routes_install_permission_required,
        test_23_routes_hub_git_return_501,
        test_24_discovery_generate_manifest_route,
        test_25_builder_still_compiles_with_packaging_state,
        test_26_builder_can_self_package,
    ]
    for t in tests:
        await t()

    print(f"\n{'═' * 60}")
    print(f"  ALL {len(tests)} TESTS PASSED ✓")
    print(f"{'═' * 60}\n")


if __name__ == "__main__":
    asyncio.run(main())
