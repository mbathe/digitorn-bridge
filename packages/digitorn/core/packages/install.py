"""InstallFlow - orchestrates the install / upgrade / uninstall lifecycle."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from digitorn.core.packages.hash import (
    compute_package_hash,
    write_package_hash_file,
)
from digitorn.core.packages.manifest import PackageManifest
from digitorn.core.packages.registry import (
    PackageRegistry,
    SourceType,
    Status,
)
from digitorn.core.packages.source import FetchError, PackageSource

logger = logging.getLogger(__name__)

# Upgrade policy: PRESERVE BY DEFAULT.
#
# `_patch_in_place` overlays every file from the new source over the
# existing install dir. Files present in the install dir but absent
# from the source tarball are NEVER touched - we don't know what an
# app may have stashed (data dir, user assets, agent-generated build
# outputs, cached deps, …). The new tarball declares what it ships;
# anything else stays.
#
# The ONLY exception is `.digitorn/` (daemon-owned metadata:
# hash.sha256 + manifest.lock). The daemon rewrites these post-patch,
# so the source's `.digitorn/` would be a stale snapshot if shipped.
_DAEMON_PRIVATE_DIR: str = ".digitorn"

def _patch_in_place(src: Path, dst: Path) -> tuple[int, int]:
    import os as _os
    import errno as _errno
    written = 0
    skipped_locked = 0
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in _os.walk(src, topdown=True):
        # Skip the daemon-private dir if the tarball happens to ship
        # one. We rewrite it post-patch with the fresh manifest+hash.
        dirs[:] = [d for d in dirs if d != _DAEMON_PRIVATE_DIR]
        rel_root = Path(root).relative_to(src)
        target_dir = dst / rel_root
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        for fname in files:
            src_file = Path(root) / fname
            target_file = target_dir / fname
            try:
                shutil.copy2(src_file, target_file)
                written += 1
            except PermissionError:
                skipped_locked += 1
            except OSError as exc:
                if getattr(exc, "errno", None) == _errno.EACCES:
                    skipped_locked += 1
                else:
                    logger.warning(
                        "patch_in_place: copy %s -> %s failed: %s",
                        src_file, target_file, exc,
                    )
    return written, skipped_locked

class InstallError(Exception):
    """Generic install/upgrade failure with a user-friendly message."""

class PermissionsRequired(Exception):
    """Raised when accept_permissions is missing - the route translates."""

    def __init__(self, perms: dict[str, Any], manifest_id: str):
        self.perms = perms
        self.manifest_id = manifest_id
        super().__init__(
            f"permissions required for package {manifest_id!r}"
        )

class PackageIdCollision(Exception):
    """Raised when a package with the same id is already installed."""

    def __init__(self, package_id: str, existing: dict[str, Any]):
        self.package_id = package_id
        self.existing = existing
        super().__init__(
            f"package {package_id!r} already installed from "
            f"source {existing.get('source_type')!r}"
        )

class IncompatibleDaemonVersion(Exception):
    """Raised when the package declares a daemon version range we."""

@dataclass
class InstallResult:
    """Returned by `InstallFlow.install` and friends."""

    package_id: str
    version: str
    source_type: str
    source_uri: str
    install_dir: str
    hash: str
    deployed: bool = False
    deploy_error: str | None = None

# Type alias for the optional deploy callback. The orchestrator
# doesn't import the AppManager directly to keep this module
# decoupled from the rest of the daemon.
DeployCallback = Callable[[Path, str], Awaitable[Any]]
UndeployCallback = Callable[[str], Awaitable[Any]]

class InstallFlow:
    def __init__(
        self,
        *,
        registry: PackageRegistry,
        source_map: dict[str, PackageSource],
        install_root: Path,
        daemon_version: str = "2.0.0",
        user_install_root: Path | None = None,
    ) -> None:
        """"""
        self._registry = registry
        self._sources = source_map
        self._install_root = install_root
        self._user_install_root = user_install_root or (
            Path.home() / ".digitorn" / "users"
        )
        self._daemon_version = daemon_version

    def _resolve_install_dir(
        self,
        package_id: str,
        *,
        scope: str,
        owner_user_id: str | None,
    ) -> Path:
        from digitorn.core.app.manager_v2._models import _scoped_slug
        from digitorn.core.packages.registry import Scope

        if scope == Scope.SYSTEM:
            slug = _scoped_slug(package_id, "system", "")
        elif scope == Scope.USER:
            if not owner_user_id:
                raise ValueError(
                    "scope='user' requires owner_user_id"
                )
            safe_owner = _safe_user_dir_name(owner_user_id)
            slug = _scoped_slug(package_id, "user", safe_owner)
        else:
            raise ValueError(f"unknown scope {scope!r}")
        return self._install_root / slug

    async def probe_permissions(
        self,
        source_type: str,
        source_uri: str,
    ) -> dict[str, Any]:
        """Fetch the package, parse its manifest, return permissions."""
        manifest, scratch_dir = await self._fetch_and_validate(
            source_type, source_uri,
        )
        return {
            "package_id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "permissions": manifest.permissions.model_dump(mode="json"),
            "credentials": manifest.credentials.model_dump(mode="json"),
            "requirements": manifest.requirements.model_dump(mode="json"),
            "compatibility": manifest.compatibility.model_dump(mode="json"),
            "scratch_dir": str(scratch_dir),
        }

    async def install(
        self,
        *,
        source_type: str,
        source_uri: str,
        installed_by: str = "",
        accept_permissions: bool = False,
        on_deploy: DeployCallback | None = None,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> InstallResult:
        """Run the full install pipeline."""
        from digitorn.core.packages.registry import Scope

        if scope not in Scope.ALL:
            raise ValueError(f"unknown scope {scope!r}")
        if scope == Scope.USER and not owner_user_id:
            raise ValueError("scope='user' requires owner_user_id")
        if scope == Scope.SYSTEM and owner_user_id:
            # Silently drop - system scope is always owner-less
            owner_user_id = None

        manifest, scratch_dir = await self._fetch_and_validate(
            source_type, source_uri,
        )

        # Compatibility check
        self._check_daemon_compat(manifest)

        # Collision check - within the same (scope, owner) tuple.
        existing = await self._registry.get(
            manifest.id, scope=scope, owner_user_id=owner_user_id,
        )
        if existing is not None:
            raise PackageIdCollision(manifest.id, existing)

        # When installing user-scope, refuse if a system-scope install
        # of the same app_id already exists. Builtins / system apps
        # are shared, no per-user duplicates needed.
        if scope == Scope.USER:
            system_exists = await self._registry.get(
                manifest.id, scope=Scope.SYSTEM, owner_user_id=None,
            )
            if system_exists is not None:
                raise PackageIdCollision(manifest.id, system_exists)

        # Permissions consent (locked design D5)
        if not accept_permissions:
            raise PermissionsRequired(
                perms=manifest.permissions.model_dump(mode="json"),
                manifest_id=manifest.id,
            )

        # Move scratch into the final install dir (atomic on the same
        # FS); all disk ops are off-loop to spare Windows + AV stalls.
        import asyncio as _asyncio
        install_dir = self._resolve_install_dir(
            manifest.id, scope=scope, owner_user_id=owner_user_id,
        )
        if await _asyncio.to_thread(install_dir.exists):
            await _asyncio.to_thread(shutil.rmtree, install_dir)
        await _asyncio.to_thread(
            install_dir.parent.mkdir, parents=True, exist_ok=True,
        )
        try:
            await _asyncio.to_thread(scratch_dir.rename, install_dir)
        except OSError:
            # Cross-FS rename -> fall back to copytree + rmtree (off-loop)
            await _asyncio.to_thread(shutil.copytree, scratch_dir, install_dir)
            await _asyncio.to_thread(shutil.rmtree, scratch_dir, True)

        # Compute + persist hash off-loop. Sha256 over every file in
        # the package = O(seconds) on big packages and would stall the
        # event loop for every other connected user during install.
        try:
            hash_value = await _asyncio.to_thread(compute_package_hash, install_dir)
            await _asyncio.to_thread(write_package_hash_file, install_dir, hash_value)
        except Exception as exc:
            logger.warning(
                "InstallFlow: hash computation failed for %s: %s",
                manifest.id, exc,
            )
            hash_value = ""

        # Persist manifest snapshot inside the install dir, off-loop.
        try:
            def _write_manifest_lock() -> None:
                (install_dir / ".digitorn").mkdir(exist_ok=True)
                (install_dir / ".digitorn" / "manifest.lock").write_text(
                    manifest.to_toml(), encoding="utf-8",
                )
            await _asyncio.to_thread(_write_manifest_lock)
        except Exception as exc:
            logger.warning(
                "InstallFlow: cannot write manifest.lock: %s", exc,
            )

        # Register in DB
        await self._registry.create(
            package_id=manifest.id,
            source_type=source_type,
            source_uri=source_uri,
            version=manifest.version,
            hash=hash_value,
            manifest=manifest.to_dict(),
            installed_by=installed_by,
            status=Status.INSTALLED,
            scope=scope,
            owner_user_id=owner_user_id,
        )

        # Optional deploy callback - pass scope/owner so the
        # AppManager can key the DeployedApp correctly.
        deployed = False
        deploy_error: str | None = None
        if on_deploy is not None:
            try:
                # New callbacks accept (yaml_path, package_id, scope,
                # owner_user_id); fall back to the 2-arg legacy shape.
                import inspect
                sig = inspect.signature(on_deploy)
                if len(sig.parameters) >= 4:
                    await on_deploy(
                        install_dir / "app.yaml",
                        manifest.id,
                        scope,
                        owner_user_id,
                    )
                else:
                    await on_deploy(install_dir / "app.yaml", manifest.id)
                deployed = True
            except Exception as exc:
                logger.exception(
                    "InstallFlow: deploy failed for %s", manifest.id,
                )
                deploy_error = str(exc)
                await self._registry.update_status(
                    manifest.id,
                    status=Status.BROKEN,
                    last_error=str(exc),
                    scope=scope,
                    owner_user_id=owner_user_id,
                )

        return InstallResult(
            package_id=manifest.id,
            version=manifest.version,
            source_type=source_type,
            source_uri=source_uri,
            install_dir=str(install_dir),
            hash=hash_value,
            deployed=deployed,
            deploy_error=deploy_error,
        )

    async def uninstall(
        self,
        package_id: str,
        *,
        force: bool = False,
        on_undeploy: UndeployCallback | None = None,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> bool:
        """Remove a package: stop the deployed app, wipe the dir, drop the row."""
        existing = await self._registry.get(
            package_id, scope=scope, owner_user_id=owner_user_id,
        )
        if existing is None:
            return False

        if existing["source_type"] == SourceType.BUILTIN and not force:
            raise InstallError(
                f"package {package_id!r} is a builtin - pass force=True to "
                f"uninstall (it will be reinstalled at the next daemon boot)"
            )

        await self._registry.update_status(
            package_id, status=Status.UNINSTALLING,
            scope=scope, owner_user_id=owner_user_id,
        )

        # Stop the deployed app
        if on_undeploy is not None:
            try:
                import inspect
                sig = inspect.signature(on_undeploy)
                if len(sig.parameters) >= 3:
                    await on_undeploy(package_id, scope, owner_user_id)
                else:
                    await on_undeploy(package_id)
            except Exception as exc:
                logger.warning(
                    "InstallFlow: undeploy failed for %s (continuing): %s",
                    package_id, exc,
                )

        # Wipe the install dir off-loop (rmtree of populated app dirs
        # routinely takes seconds; the daemon stalls for everyone otherwise).
        import asyncio as _asyncio
        install_dir = Path(existing["install_dir"])
        if await _asyncio.to_thread(install_dir.exists):
            try:
                def _wipe() -> None:
                    if install_dir.is_symlink():
                        install_dir.unlink()
                    else:
                        shutil.rmtree(install_dir)
                await _asyncio.to_thread(_wipe)
            except Exception as exc:
                logger.warning(
                    "InstallFlow: failed to remove %s: %s", install_dir, exc,
                )

        # Drop the registry row
        return await self._registry.delete(
            package_id, scope=scope, owner_user_id=owner_user_id,
        )

    async def upgrade(
        self,
        package_id: str,
        *,
        source_type: str,
        source_uri: str,
        accept_permissions: bool = False,
        installed_by: str = "",
        on_deploy: DeployCallback | None = None,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> InstallResult:
        """Replace an installed package with a new version."""
        existing = await self._registry.get(
            package_id, scope=scope, owner_user_id=owner_user_id,
        )
        if existing is None:
            raise InstallError(
                f"package {package_id!r} is not installed - use install instead"
            )

        existing_dir = (existing.get("install_dir") or "").strip()
        if not existing_dir or existing_dir in (".", "./"):
            logger.warning(
                "InstallFlow.upgrade: registry row for %s has empty "
                "install_dir - falling back to fresh install",
                package_id,
            )
            await self._registry.delete(
                package_id, scope=scope, owner_user_id=owner_user_id,
            )
            return await self.install(
                source_type=source_type,
                source_uri=source_uri,
                accept_permissions=accept_permissions,
                installed_by=installed_by,
                on_deploy=on_deploy,
                scope=scope,
                owner_user_id=owner_user_id,
            )

        new_manifest, scratch_dir = await self._fetch_and_validate(
            source_type, source_uri,
        )
        if new_manifest.id != package_id:
            raise InstallError(
                f"upgrade target id {new_manifest.id!r} doesn't match "
                f"installed id {package_id!r}"
            )

        self._check_daemon_compat(new_manifest)

        if not accept_permissions:
            raise PermissionsRequired(
                perms=new_manifest.permissions.model_dump(mode="json"),
                manifest_id=new_manifest.id,
            )

        # Off-load every shutil op: `rmtree` over a populated install
        # dir can stall the loop for tens of seconds on Windows.
        import asyncio as _asyncio
        install_dir = Path(existing["install_dir"])
        old_dir = install_dir.with_name(install_dir.name + "-old")
        new_dir = install_dir.with_name(install_dir.name + "-new")

        # Move scratch -> new_dir for atomic swap. Every stat / rename
        # off-loop because each one can spike to hundreds of ms on
        # Windows + antivirus.
        if await _asyncio.to_thread(new_dir.exists):
            await _asyncio.to_thread(shutil.rmtree, new_dir)
        try:
            await _asyncio.to_thread(scratch_dir.rename, new_dir)
        except OSError:
            await _asyncio.to_thread(shutil.copytree, scratch_dir, new_dir)
            await _asyncio.to_thread(shutil.rmtree, scratch_dir, True)

        await self._registry.update_status(
            package_id, status=Status.UPGRADING,
            scope=scope, owner_user_id=owner_user_id,
        )

        # Clear any stale `-old` leftover from a previous failed upgrade.
        if await _asyncio.to_thread(old_dir.exists):
            await _asyncio.to_thread(shutil.rmtree, old_dir, True)

        # Full backup of the current install dir into `old_dir` so a
        # failed upgrade can roll back 1:1, including any app state.
        def _backup_copy() -> None:
            shutil.copytree(
                install_dir, old_dir,
                dirs_exist_ok=False,
            )
        try:
            await _asyncio.to_thread(_backup_copy)
        except Exception as exc:
            # Backup failure isn't fatal - the patch-in-place is still
            # safe on a reasonably atomic FS - but we lose rollback.
            logger.warning(
                "InstallFlow.upgrade: V1 backup to %s failed (%s); "
                "rollback will not be possible if V2 deploy fails",
                old_dir, exc,
            )

        try:
            written, skipped = await _asyncio.wait_for(
                _asyncio.to_thread(_patch_in_place, new_dir, install_dir),
                timeout=20.0,
            )
            logger.info(
                "InstallFlow.upgrade: %s patched in place (%d written, %d skipped due to locks)",
                package_id, written, skipped,
            )
        except _asyncio.TimeoutError:
            logger.error(
                "InstallFlow.upgrade: in-place patch timed out for %s - "
                "previous install remains active, daemon continues",
                package_id,
            )
            await _asyncio.to_thread(shutil.rmtree, new_dir, True)
            raise InstallError(
                f"upgrade patch timed out for {package_id}"
            )
        finally:
            await _asyncio.to_thread(shutil.rmtree, new_dir, True)

        # Update hash + manifest off-loop (sync sha256 of every file).
        import asyncio as _asyncio
        try:
            hash_value = await _asyncio.to_thread(compute_package_hash, install_dir)
            await _asyncio.to_thread(write_package_hash_file, install_dir, hash_value)
        except Exception:
            hash_value = ""

        await self._registry.update_version(
            package_id,
            new_version=new_manifest.version,
            new_hash=hash_value,
            new_manifest=new_manifest.to_dict(),
            scope=scope,
            owner_user_id=owner_user_id,
        )

        # Redeploy
        deploy_error: str | None = None
        deployed = False
        if on_deploy is not None:
            try:
                import inspect
                sig = inspect.signature(on_deploy)
                if len(sig.parameters) >= 4:
                    await on_deploy(
                        install_dir / "app.yaml",
                        package_id,
                        scope,
                        owner_user_id,
                    )
                else:
                    await on_deploy(install_dir / "app.yaml", package_id)
                deployed = True
                # Success - delete the old version off-loop
                if await _asyncio.to_thread(old_dir.exists):
                    await _asyncio.to_thread(shutil.rmtree, old_dir, True)
            except Exception as exc:
                logger.exception(
                    "InstallFlow: upgrade deploy failed for %s - rolling back",
                    package_id,
                )
                deploy_error = str(exc)
                # Rollback: swap dirs back AND revert registry metadata to V1
                # so disk and registry stay consistent.
                try:
                    await _asyncio.to_thread(shutil.rmtree, install_dir, True)
                    if await _asyncio.to_thread(old_dir.exists):
                        await _asyncio.to_thread(old_dir.rename, install_dir)

                    # Rebuild V1 metadata from the restored manifest
                    # + recomputed hash so the registry matches disk.
                    v1_manifest = None
                    v1_hash = ""
                    try:
                        v1_manifest, _ = await self._fetch_and_validate(
                            SourceType.LOCAL, str(install_dir),
                        )
                        v1_hash = await _asyncio.to_thread(
                            compute_package_hash, install_dir,
                        )
                        await _asyncio.to_thread(
                            write_package_hash_file, install_dir, v1_hash,
                        )
                    except Exception as introspect_exc:
                        logger.warning(
                            "InstallFlow.upgrade rollback: V1 re-introspection "
                            "failed for %s (%s); registry may show stale V2 "
                            "version/hash until next reinstall",
                            package_id, introspect_exc,
                        )

                    if v1_manifest is not None:
                        await self._registry.update_version(
                            package_id,
                            new_version=v1_manifest.version,
                            new_hash=v1_hash,
                            new_manifest=v1_manifest.to_dict(),
                            scope=scope,
                            owner_user_id=owner_user_id,
                        )
                    await self._registry.update_status(
                        package_id,
                        status=Status.INSTALLED,
                        last_error=f"upgrade rolled back: {exc}",
                        scope=scope,
                        owner_user_id=owner_user_id,
                    )
                except Exception as rollback_exc:
                    logger.error(
                        "InstallFlow: rollback ALSO failed for %s: %s",
                        package_id, rollback_exc,
                    )
                    await self._registry.update_status(
                        package_id,
                        status=Status.BROKEN,
                        last_error=(
                            f"upgrade failed: {exc}; rollback failed: {rollback_exc}"
                        ),
                    )

        return InstallResult(
            package_id=package_id,
            version=new_manifest.version,
            source_type=source_type,
            source_uri=source_uri,
            install_dir=str(install_dir),
            hash=hash_value,
            deployed=deployed,
            deploy_error=deploy_error,
        )

    async def _fetch_and_validate(
        self,
        source_type: str,
        source_uri: str,
    ) -> tuple[PackageManifest, Path]:
        source = self._sources.get(source_type)
        if source is None:
            raise InstallError(
                f"unknown source_type {source_type!r}. "
                f"Available: {list(self._sources)}"
            )

        import asyncio as _asyncio
        scratch_root = self._install_root / ".tmp"
        await _asyncio.to_thread(
            scratch_root.mkdir, parents=True, exist_ok=True,
        )
        # Use a stable name so probe_permissions and install share
        # the same scratch dir. urllib-quote-style sanitisation.
        safe_uri = source_uri.replace("/", "_").replace(":", "_")[:128]
        scratch_dir = scratch_root / f"{source_type}-{safe_uri}"

        try:
            package_dir = await source.fetch(source_uri, scratch_dir)
        except FetchError:
            raise
        except NotImplementedError as exc:
            # Hub / git stubs land here in v1
            raise InstallError(str(exc)) from exc

        toml_path = package_dir / "package.toml"
        try:
            manifest = await _asyncio.to_thread(PackageManifest.from_path, toml_path)
        except Exception as exc:
            import asyncio as _asyncio
            await _asyncio.to_thread(shutil.rmtree, scratch_dir, True)
            raise InstallError(
                f"manifest validation failed: {exc}"
            ) from exc

        return manifest, package_dir

    def _check_daemon_compat(self, manifest: PackageManifest) -> None:
        compat = manifest.compatibility
        if not compat.digitorn_min and not compat.digitorn_max:
            return
        ours = self._daemon_version
        if compat.digitorn_min and not _satisfies(ours, compat.digitorn_min):
            raise IncompatibleDaemonVersion(
                f"package {manifest.id!r} requires Digitorn {compat.digitorn_min}, "
                f"running {ours}"
            )
        if compat.digitorn_max and not _satisfies(ours, compat.digitorn_max):
            raise IncompatibleDaemonVersion(
                f"package {manifest.id!r} requires Digitorn {compat.digitorn_max}, "
                f"running {ours}"
            )

def _safe_user_dir_name(user_id: str) -> str:
    import re
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id or "")
    return safe.strip("_") or "user"

def _satisfies(version: str, requirement: str) -> bool:
    import re as _re

    m = _re.match(r"^\s*(>=|<=|>|<|==|!=|~=)?\s*([\w.+-]+)$", requirement)
    if not m:
        return True
    op = m.group(1) or ">="
    target = m.group(2)

    def _parse(v: str) -> tuple[int, int, int]:
        parts = (v.split("-")[0].split("+")[0]).split(".")
        try:
            return tuple(int(p) for p in (parts + ["0", "0", "0"])[:3])
        except ValueError:
            return (0, 0, 0)

    a = _parse(version)
    b = _parse(target)

    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == "~=":
        # Compatible release: same major.minor, patch >=
        return a[0] == b[0] and a[1] == b[1] and a[2] >= b[2]
    return True
