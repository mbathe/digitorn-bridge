"""InstallFlow - orchestrates the install / upgrade / uninstall lifecycle.

This is the **only** module that knows the full sequence of steps
to install a package. Routes call into it; sources don't talk to it.
This single-place-of-orchestration makes the lifecycle testable
without spinning up a daemon.

The flow is **failure-safe at every step**: the install dir is only
moved into place after the manifest validates AND the daemon
compatibility checks pass AND the user has accepted permissions
AND there's no existing package with the same id. If anything
fails before the move, no state is changed.

Public API::

    flow = InstallFlow(registry=..., source_map={...}, install_root=...)

    # Install
    result = await flow.install(
        source_type="local",
        source_uri="/path/to/my-app",
        installed_by="alice",
        accept_permissions=True,
        on_deploy=manager.deploy,  # optional callable
    )

    # Probe permissions (no install)
    perms = await flow.probe_permissions("local", "/path/to/my-app")

    # Uninstall
    await flow.uninstall("my-app", on_undeploy=manager.undeploy)

    # Upgrade
    await flow.upgrade("my-app", source_uri="/new/path", ...)
"""

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


# Directories whose CONTENT we never want to overwrite during an
# upgrade. Split into two tiers because the same directory name can
# legitimately mean different things at different depths:
#
# - ``_PRESERVE_DIRS_ANY_DEPTH``: caches that should never be copied
#   regardless of where they live (every nested ``__pycache__``,
#   every ``.cache``, every ``.git``).
#
# - ``_PRESERVE_DIRS_AT_ROOT``: build outputs / dependency caches that
#   only need preservation when they sit at the package root (or under
#   ``web/`` — the typical Vite/Next layout). When a directory of the
#   same name sits DEEPER (e.g. ``templates/landing-ai-saas/dist/``
#   in ``digitorn-lovable``, which is SHIPPED preview content, not a
#   live build output), we MUST copy it through. The previous flat
#   set caused every nested ``dist/`` in lovable's templates to be
#   silently skipped on upgrade, leaving the iframe previews 404.
_PRESERVE_DIRS_AT_ROOT: frozenset[str] = frozenset({
    "node_modules",
    "dist",
    "build",
})
_PRESERVE_DIRS_ANY_DEPTH: frozenset[str] = frozenset({
    ".vite",
    ".next",
    ".turbo",
    ".cache",
    "__pycache__",
    ".output",
    ".svelte-kit",
    ".digitorn",
})
# Backwards-compatible union kept for legacy callers
# (``shutil.ignore_patterns(*_PRESERVE_DIRS)`` etc.) — covers both
# tiers. New code paths should prefer the depth-aware split.
_PRESERVE_DIRS: frozenset[str] = _PRESERVE_DIRS_AT_ROOT | _PRESERVE_DIRS_ANY_DEPTH


def _patch_in_place(src: Path, dst: Path) -> tuple[int, int]:
    """Copy every file in ``src`` over the matching path in ``dst``.

    Used in lieu of an atomic rename swap when the install dir is held
    open on Windows (Vite, antivirus, file watcher). We never rename or
    delete ``dst`` itself - only individual files are overwritten in
    place. Existing files in ``dst`` that aren't in ``src`` are left
    untouched (safer than aggressive pruning).

    Two-tier preservation (see ``_PRESERVE_DIRS_AT_ROOT`` /
    ``_PRESERVE_DIRS_ANY_DEPTH``):
      - ``dist`` / ``build`` / ``node_modules`` are preserved ONLY when
        sitting at the package root or directly under ``web/``. A
        nested ``templates/<id>/dist/`` is treated as shipped content
        and copied through.
      - ``.vite``, ``__pycache__``, ``.digitorn``, … are caches that
        get preserved at any depth.

    Returns ``(written, skipped_due_to_lock)``.
    """
    import os as _os
    import errno as _errno
    written = 0
    skipped_locked = 0
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in _os.walk(src, topdown=True):
        # Compute depth from src to know whether root-level
        # preservation rules apply. ``rel_root`` is the path of the
        # current directory relative to ``src``; depth 0 == src itself.
        rel_root_str = _os.path.relpath(root, src)
        is_root_or_web = rel_root_str in (".", "web")
        kept_dirs: list[str] = []
        for d in dirs:
            if d in _PRESERVE_DIRS_ANY_DEPTH:
                continue  # cache dir, never copy
            if d in _PRESERVE_DIRS_AT_ROOT and is_root_or_web:
                continue  # build output at root/web/ — preserve target
            kept_dirs.append(d)
        dirs[:] = kept_dirs
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
                        "patch_in_place: copy %s → %s failed: %s",
                        src_file, target_file, exc,
                    )
    return written, skipped_locked


# ────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────


class InstallError(Exception):
    """Generic install/upgrade failure with a user-friendly message."""


class PermissionsRequired(Exception):
    """Raised when accept_permissions is missing - the route translates
    this into a 409 with the perms payload so the client can show a
    confirmation dialog."""

    def __init__(self, perms: dict[str, Any], manifest_id: str):
        self.perms = perms
        self.manifest_id = manifest_id
        super().__init__(
            f"permissions required for package {manifest_id!r}"
        )


class PackageIdCollision(Exception):
    """Raised when a package with the same id is already installed -
    locked design D12 says we refuse rather than overwrite."""

    def __init__(self, package_id: str, existing: dict[str, Any]):
        self.package_id = package_id
        self.existing = existing
        super().__init__(
            f"package {package_id!r} already installed from "
            f"source {existing.get('source_type')!r}"
        )


class IncompatibleDaemonVersion(Exception):
    """Raised when the package declares a daemon version range we
    don't satisfy."""


# ────────────────────────────────────────────────────────────────────
# Result dataclass
# ────────────────────────────────────────────────────────────────────


@dataclass
class InstallResult:
    """Returned by ``InstallFlow.install`` and friends."""

    package_id: str
    version: str
    source_type: str
    source_uri: str
    install_dir: str
    hash: str
    deployed: bool = False
    deploy_error: str | None = None


# ────────────────────────────────────────────────────────────────────
# The orchestrator
# ────────────────────────────────────────────────────────────────────


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
        """Args:
            registry: PackageRegistry instance
            source_map: source_type → PackageSource (e.g. {"local": LocalSource()})
            install_root: where system-scoped packages live (~/.digitorn/packages)
            daemon_version: current daemon semver, used for compat checks
            user_install_root: base dir for user-scoped installs. Each
                user gets a subdir under here
                (``<user_install_root>/<user_id>/packages/<id>/``).
                Defaults to ``~/.digitorn/users``.
        """
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
        """Return the on-disk directory where a package lives.

        Deterministic layout under ``~/.digitorn/apps/``:
          - System: ``~/.digitorn/apps/<package_id>/``
          - User:   ``~/.digitorn/apps/_@<uid>__<package_id>/``
        """
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

    # ── Permissions probe ───────────────────────────────────────

    async def probe_permissions(
        self,
        source_type: str,
        source_uri: str,
    ) -> dict[str, Any]:
        """Fetch the package, parse its manifest, return permissions.

        Used by the HTTP route's "no accept_permissions" path: the
        client gets back the permissions dict, shows a dialog, then
        re-calls install with accept_permissions=true.

        Side effect: a temporary copy is created in
        ``install_root/.tmp/<id>/`` so the next install call can
        reuse it. This avoids fetching twice.
        """
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

    # ── Install ─────────────────────────────────────────────────

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
        """Run the full install pipeline.

        Steps:

        1. Fetch package into a scratch dir (source.fetch)
        2. Parse + validate package.toml
        3. Check daemon version compat
        4. Check id collision WITHIN THIS SCOPE
           (a system install and a user install of the same
           package_id can coexist)
        5. If accept_permissions=False → raise PermissionsRequired
        6. Move scratch dir → resolved install dir (atomic rename)
        7. Compute + write content hash
        8. Insert row in registry with scope + owner_user_id
        9. Call on_deploy(install_dir/app.yaml, package_id)
        10. Return InstallResult
        """
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

        # Move scratch -> final install dir (atomic on the same FS).
        # All disk ops off-loop: rmtree / rename / mkdir / write on Windows
        # with antivirus can each stall hundreds of ms per call, and with
        # bootstrap_builtins running 4 installs in parallel that would
        # block every other connected user during the whole boot.
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
                # Newer deploy callbacks accept (yaml_path, package_id,
                # scope, owner_user_id) - fall back to (yaml_path,
                # package_id) for legacy callbacks so we don't break
                # existing wiring.
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

    # ── Uninstall ───────────────────────────────────────────────

    async def uninstall(
        self,
        package_id: str,
        *,
        force: bool = False,
        on_undeploy: UndeployCallback | None = None,
        scope: str = "system",
        owner_user_id: str | None = None,
    ) -> bool:
        """Remove a package: stop the deployed app, wipe the dir, drop the row.

        Scope-aware: two installs of the same package_id (one
        system, one per-user) are distinct - uninstalling one does
        not touch the other.

        Refuses with InstallError if the package is a builtin and
        ``force=False`` (locked design D9).
        """
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

    # ── Upgrade ─────────────────────────────────────────────────

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
        """Replace an installed package with a new version.

        Strategy:
        - Fetch the new version into ``<install_dir>-new/``
        - Validate, check compat, check perms
        - Rename current install dir → ``<install_dir>-old/``
        - Rename new dir → ``<install_dir>/``
        - Update registry version + hash
        - Call deploy
        - On success: delete ``<install_dir>-old/``
        - On failure: roll back (swap dirs back, redeploy old)
        """
        existing = await self._registry.get(
            package_id, scope=scope, owner_user_id=owner_user_id,
        )
        if existing is None:
            raise InstallError(
                f"package {package_id!r} is not installed - use install instead"
            )

        # Defensive: a registry row can be in a half-broken state where
        # ``install_dir`` is empty / "." (legacy bug, dev-CLI deploy that
        # never wrote the column, etc). The dir-swap math below crashes
        # on ``Path('').name``, so detect this and re-route to a fresh
        # install which writes the column correctly.
        existing_dir = (existing.get("install_dir") or "").strip()
        if not existing_dir or existing_dir in (".", "./"):
            logger.warning(
                "InstallFlow.upgrade: registry row for %s has empty "
                "install_dir — falling back to fresh install",
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

        # All shutil ops below run off the event loop. The bootstrap
        # path that triggers this (``bootstrap_builtins`` →
        # ``_upgrade_builtin``) has been observed stalling the main
        # loop for 17+ seconds via ``shutil.rmtree`` of populated app
        # dirs. Off-loading restores Socket.IO ping/pong cadence and
        # lets the agent loop complete turns while builtins upgrade.
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

        # Clear any stale ``-old`` leftover from a previous failed upgrade.
        if await _asyncio.to_thread(old_dir.exists):
            await _asyncio.to_thread(shutil.rmtree, old_dir, True)

        # BACKUP current V1 -> old_dir so we can roll back if the new V2
        # deploy fails. Without this, ``_patch_in_place`` would mutate
        # install_dir irreversibly and the rollback branch below would
        # rename an empty/nonexistent old_dir back, leaving the app in
        # an inconsistent state. We copy (not rename) so any open file
        # handle into install_dir - think Vite watcher, antivirus scan,
        # Windows Indexer - doesn't block the upgrade at step zero.
        def _backup_copy() -> None:
            shutil.copytree(
                install_dir, old_dir,
                ignore=shutil.ignore_patterns(*_PRESERVE_DIRS),
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
                # Rollback: swap dirs back AND revert registry metadata
                # (version + hash + manifest) to V1. Without this, the
                # registry keeps the V2 values set a few lines above via
                # update_version() even though disk has been restored to
                # V1 - the client would see a version/hash drift on
                # every GET /api/apps/{id}.
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

    # ── Internals ───────────────────────────────────────────────

    async def _fetch_and_validate(
        self,
        source_type: str,
        source_uri: str,
    ) -> tuple[PackageManifest, Path]:
        """Fetch into a scratch dir + validate the manifest.

        Returns (manifest, scratch_dir). The scratch dir is inside
        ``install_root/.tmp/`` and the caller is responsible for
        either moving it into place or cleaning it up.
        """
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
        """Reject if the package declares a daemon version range we don't satisfy.

        Best-effort: we only honour ``digitorn_min`` and ``digitorn_max``
        in v1. Full PEP 440 / semver range matching can come later.
        """
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


# ────────────────────────────────────────────────────────────────────
# Tiny semver range checker - enough for >=, <=, >, <, ==, !=
# ────────────────────────────────────────────────────────────────────


def _safe_user_dir_name(user_id: str) -> str:
    """Sanitize a user_id into a safe directory name.

    Strips path separators and keeps only chars that are safe
    everywhere (letters, digits, dash, underscore). Falls back to
    "user" if the result is empty. Path traversal via the
    owner_user_id is blocked at this layer.
    """
    import re
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user_id or "")
    return safe.strip("_") or "user"


def _satisfies(version: str, requirement: str) -> bool:
    """Naive semver range check: parses ``op + version`` and compares.

    Returns True when ``version`` satisfies ``requirement``. Falls
    back to True on parse errors (be lenient - better to install
    a maybe-incompatible package than to refuse a valid one because
    of a string format quirk).
    """
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
