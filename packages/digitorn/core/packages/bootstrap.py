"""Built-in package bootstrap - runs once at daemon startup.

This is the **only** thing that auto-installs anything on the
daemon. It scans ``packages/digitorn/builtins/`` (the directory
shipped inside the wheel), and for each package it finds:

- If the package isn't installed yet → install it
- If the package IS installed but its content hash has changed
  (e.g. ``pip install -U digitorn`` brought a newer version) →
  upgrade it
- If the package is installed AND the hash matches → no-op

The flow is **silent on success** and **never blocks daemon
startup on failure**: a single broken built-in shouldn't take
down the whole daemon. Failures are logged + the package is
marked ``broken`` in the registry so the UI can surface it.

Wired into ``server.py`` lifespan after ``app_manager`` is built
and the credential store is initialised.

Locked design references:

- D2 (hash strategy) - used to detect upgrades
- D9 (builtin protection) - bootstrap auto-installs even if a
  user previously did ``--force`` uninstall
- D11 (install permission) - bypassed for built-ins because
  bootstrap runs as the daemon process, not on behalf of a user
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from digitorn.core.packages.install import (
    InstallError,
    InstallFlow,
    PackageIdCollision,
    PermissionsRequired,
)
from digitorn.core.packages.registry import (
    PackageRegistry,
    SourceType,
    Status,
)
from digitorn.core.packages.sources.builtin import BuiltinSource

logger = logging.getLogger(__name__)


# Installed packages live here, one deterministic dir per
# (app_id, scope, owner_user_id). Override via the install_root
# argument when running tests with a tmp dir.
DEFAULT_INSTALL_ROOT = Path.home() / ".digitorn" / "apps"


# Inside the wheel - the directory shipped with `pip install digitorn`
def _default_builtins_dir() -> Path:
    """Return the absolute path to the built-in packages directory.

    Lives next to this module's parent: ``packages/digitorn/builtins/``.
    """
    return Path(__file__).resolve().parent.parent.parent / "builtins"


async def bootstrap_builtins(
    *,
    registry: PackageRegistry,
    on_deploy: Any = None,
    install_root: Path | None = None,
    builtins_dir: Path | None = None,
    daemon_version: str = "2.0.0",
) -> dict[str, Any]:
    """Install / upgrade every built-in package shipped with the daemon.

    Args:
        registry: PackageRegistry instance backed by the daemon DB
        on_deploy: optional callback (path, package_id) → awaitable
            that will be called for each freshly installed package.
            Usually ``manager.deploy`` - runs the YAML through the
            compiler and registers the resulting app for activation.
        install_root: where to put the installed packages on disk.
            Defaults to ``~/.digitorn/packages/``.
        builtins_dir: where to read the built-in source from.
            Defaults to the directory shipped inside the wheel.
        daemon_version: current daemon semver, used for compat checks.

    Returns:
        A summary dict::

            {
                "installed": ["digitorn-chat", ...],
                "upgraded": ["digitorn-builder"],
                "skipped": ["digitorn-code"],
                "failed": [{"id": "...", "error": "..."}],
            }

    Never raises - every failure is captured in ``failed``.
    """
    builtins_dir = builtins_dir or _default_builtins_dir()
    install_root = install_root or DEFAULT_INSTALL_ROOT

    summary: dict[str, Any] = {
        "installed": [],
        "upgraded": [],
        "skipped": [],
        "failed": [],
    }

    if not builtins_dir.is_dir():
        logger.info(
            "bootstrap_builtins: %s does not exist - no built-ins to install",
            builtins_dir,
        )
        return summary

    source = BuiltinSource(builtins_dir)
    try:
        available = await source.list_available()
    except Exception as exc:
        logger.error(
            "bootstrap_builtins: cannot scan built-ins dir %s: %s",
            builtins_dir, exc,
        )
        return summary

    if not available:
        logger.info(
            "bootstrap_builtins: no built-in packages found in %s",
            builtins_dir,
        )
        return summary

    flow = InstallFlow(
        registry=registry,
        source_map={"builtin": source},
        install_root=install_root,
        daemon_version=daemon_version,
    )

    from digitorn.core.packages.registry import Scope

    import asyncio as _asyncio
    # Per-package install/upgrade ceiling. The hot path is:
    #   1. ``shutil.copytree`` of the builtin's package dir -- can be
    #      100+ MB for apps that ship a built ``web/`` bundle
    #      (digitorn-lovable, digitorn-builder) and slow on Windows
    #      with antivirus scanning every file.
    #   2. compile + ``build_and_set_index`` which loads fastembed/ONNX
    #      and runs it over every tool description -- 5-30s cold.
    # 60s was too tight for digitorn-lovable in practice (observed
    # "timed out after 60s - marked broken"). 5 min covers any
    # realistic builtin including ones with sizable web bundles, and
    # the daemon doesn't block on this: ``bootstrap_builtins`` runs
    # as a fire-and-forget task so a slow install never delays the
    # uvicorn ``ready`` signal.
    _PER_PKG_TIMEOUT = 300.0
    # Bound the parallel fan-out so we don't saturate Windows IO with
    # antivirus scanning N concurrent copytrees, and keep the SQLite
    # writer queue bounded. 4 is a sweet spot: 5-10 builtins finish
    # in ~one-third the wall time, no IO storm.
    _PARALLELISM = 4

    sem = _asyncio.Semaphore(_PARALLELISM)

    async def _process_pkg(pkg: Any) -> None:
        async with sem:
            try:
                existing = await registry.get(
                    pkg.package_id, scope=Scope.SYSTEM,
                )

                install_dir_missing = False
                if existing is not None:
                    _idir = existing.get("install_dir") or ""

                    def _dir_is_missing_or_empty(p: str) -> bool:
                        if not p:
                            return True
                        path = Path(p)
                        if not path.is_dir():
                            return True
                        try:
                            return not any(path.iterdir())
                        except OSError:
                            return True

                    # Off-loop: stat + readdir of a freshly-deleted antivirus-
                    # scanned dir on Windows can spike to tens of ms each.
                    install_dir_missing = await _asyncio.to_thread(
                        _dir_is_missing_or_empty, _idir,
                    )
                    if install_dir_missing:
                        logger.warning(
                            "bootstrap_builtins: %s registry says installed at "
                            "%r but the dir is missing/empty - forcing reinstall",
                            pkg.package_id, _idir,
                        )

                was_broken = (
                    existing is not None
                    and (existing.get("status") or "") == "broken"
                )
                if was_broken:
                    logger.warning(
                        "bootstrap_builtins: %s is in BROKEN state "
                        "(last_error=%r) - retrying install",
                        pkg.package_id, existing.get("last_error"),
                    )

                if existing is None or install_dir_missing or was_broken:
                    logger.info(
                        "bootstrap_builtins: installing %s v%s",
                        pkg.package_id, pkg.version,
                    )
                    await _asyncio.wait_for(
                        _install_builtin(
                            flow=flow, source=source, pkg=pkg, on_deploy=on_deploy,
                        ),
                        timeout=_PER_PKG_TIMEOUT,
                    )
                    summary["installed"].append(pkg.package_id)

                elif existing["hash"] != pkg.hash:
                    logger.info(
                        "bootstrap_builtins: upgrading %s from hash %s -> %s",
                        pkg.package_id,
                        (existing["hash"] or "")[:12],
                        pkg.hash[:12],
                    )
                    await _asyncio.wait_for(
                        _upgrade_builtin(
                            flow=flow, source=source, pkg=pkg,
                            existing=existing, on_deploy=on_deploy,
                        ),
                        timeout=_PER_PKG_TIMEOUT,
                    )
                    summary["upgraded"].append(pkg.package_id)

                else:
                    summary["skipped"].append(pkg.package_id)

            except _asyncio.TimeoutError:
                logger.error(
                    "bootstrap_builtins: %s timed out after %.0fs - marked broken, "
                    "daemon boot continues",
                    pkg.package_id, _PER_PKG_TIMEOUT,
                )
                summary["failed"].append({
                    "id": pkg.package_id,
                    "error": f"timeout after {_PER_PKG_TIMEOUT}s",
                })
                try:
                    sys_row = await registry.get(pkg.package_id, scope=Scope.SYSTEM)
                    if sys_row:
                        await registry.update_status(
                            pkg.package_id,
                            status=Status.BROKEN,
                            last_error=f"bootstrap timeout after {_PER_PKG_TIMEOUT}s",
                            scope=Scope.SYSTEM,
                        )
                except Exception:
                    pass

            except Exception as exc:
                logger.error(
                    "bootstrap_builtins: failed to install %s: %s",
                    pkg.package_id, exc, exc_info=True,
                )
                summary["failed"].append({
                    "id": pkg.package_id,
                    "error": str(exc),
                })
                try:
                    sys_row = await registry.get(
                        pkg.package_id, scope=Scope.SYSTEM,
                    )
                    if sys_row:
                        await registry.update_status(
                            pkg.package_id,
                            status=Status.BROKEN,
                            last_error=str(exc),
                            scope=Scope.SYSTEM,
                        )
                except Exception:
                    pass

    await _asyncio.gather(
        *(_process_pkg(pkg) for pkg in available),
        return_exceptions=False,
    )

    if summary["installed"] or summary["upgraded"] or summary["failed"]:
        logger.info(
            "bootstrap_builtins: installed=%d upgraded=%d skipped=%d failed=%d",
            len(summary["installed"]),
            len(summary["upgraded"]),
            len(summary["skipped"]),
            len(summary["failed"]),
        )

    return summary


async def _install_builtin(
    *,
    flow: InstallFlow,
    source: BuiltinSource,
    pkg: Any,
    on_deploy: Any,
) -> None:
    """Install a built-in WITHOUT the user-consent gate.

    Built-ins are pre-approved by virtue of shipping with the
    daemon binary. The user accepted all daemon permissions when
    they installed Digitorn - they don't need to re-consent for
    each built-in app. This is per locked design D11 (install
    permission) which exempts built-ins from per-package consent.
    """
    from digitorn.core.packages.registry import Scope

    try:
        await flow.install(
            source_type=SourceType.BUILTIN,
            source_uri=pkg.source_uri,
            installed_by="",  # daemon, not a user
            accept_permissions=True,  # auto-accept for built-ins
            on_deploy=on_deploy,
            scope=Scope.SYSTEM,  # builtins are always system-wide
        )
    except PackageIdCollision:
        # Theoretically possible if a previous bootstrap left a row
        # behind. Force-replace by deleting ONLY the system row
        # (user shadows of the same package_id are left alone).
        logger.warning(
            "bootstrap_builtins: stale row for %s - removing and retrying",
            pkg.package_id,
        )
        await flow._registry.delete(
            pkg.package_id, scope=Scope.SYSTEM,
        )
        await flow.install(
            source_type=SourceType.BUILTIN,
            source_uri=pkg.source_uri,
            installed_by="",
            accept_permissions=True,
            on_deploy=on_deploy,
            scope=Scope.SYSTEM,
        )


async def _upgrade_builtin(
    *,
    flow: InstallFlow,
    source: BuiltinSource,
    pkg: Any,
    existing: dict[str, Any],
    on_deploy: Any,
) -> None:
    """Upgrade a built-in to its new wheel-shipped version.

    Uses the regular ``InstallFlow.upgrade`` which has the
    automatic rollback on deploy failure (locked design D8).
    """
    from digitorn.core.packages.registry import Scope
    try:
        await flow.upgrade(
            existing["package_id"],
            source_type=SourceType.BUILTIN,
            source_uri=pkg.source_uri,
            accept_permissions=True,
            installed_by="",
            on_deploy=on_deploy,
            scope=Scope.SYSTEM,
        )
    except PermissionsRequired:
        # Should never happen since accept_permissions=True, but
        # defensive: log and skip.
        logger.warning(
            "bootstrap_builtins: PermissionsRequired raised on auto-upgrade "
            "of %s - skipping",
            existing["package_id"],
        )
    except InstallError as exc:
        # Upgrade failed but the rollback inside InstallFlow.upgrade
        # already restored the previous version. Just log.
        logger.warning(
            "bootstrap_builtins: upgrade of %s failed (rolled back): %s",
            existing["package_id"], exc,
        )
        raise
