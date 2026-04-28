"""Installation lifecycle routes - formerly under ``/api/packages/*``.

Consolidated under ``/api/apps/*`` on 2026-04-21 so the Flutter client
has a single mental model (``app``) instead of juggling both
``package`` and ``app`` entities.

Four routes covering the full lifecycle of an installed app:

    POST   /api/apps/install                   - install (with permissions probe)
    POST   /api/apps/{app_id}/upgrade          - upgrade
    POST   /api/apps/{app_id}/uninstall        - uninstall
    GET    /api/apps/{app_id}/check-update     - content drift report

Listing + single-app detail are handled by the pre-existing
``GET /api/apps`` + ``GET /api/apps/{app_id}`` routes in ``apps.py``,
which now include install-status + runtime-status fields.
Assets and icon are also served by ``apps.py`` routes
(``GET /{app_id}/assets/{path}``, ``GET /{app_id}/icon``) - those
handle both the deployed-bundle path and the package-install-dir
fallback, which is richer than what we'd duplicate here.

Locked design references:

- D5  - permissions consent flow (409 with payload, then re-call)
- D9  - built-in uninstall protection (admin + force)
- D11 - install permission gating
- D12 - id collision is a strict refusal (409)

Hub and git source paths return 501 - their concrete implementations
are deferred to v2 (per §14 of the design doc).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from digitorn.core.api.apps_v2 import AppResponse, _get_manager
# Helpers + request models are kept in ``api/packages.py`` as a shared
# library layer (restored 2026-04-21 after the HTTP routes moved here).
# Importing from there - instead of re-declaring - keeps a single
# source of truth and prevents drift on the InstallRequest contract.
from digitorn.core.api.packages import (
    InstallRequest,
    UninstallRequest,
    UpgradeRequest,
    _build_install_flow,
    _caller_is_admin,
    _caller_user_id,
    _get_registry,
    _resolve_deploy_callback,
)
from digitorn.core.packages import (
    InstallError,
    PackageIdCollision,
    PermissionsRequired,
    SourceType,
)

logger = logging.getLogger(__name__)

# Mounted under /api/apps so every installation operation shares the
# same namespace as deploy/disable/enable/sessions/etc.
router = APIRouter(prefix="/api/apps", tags=["apps"])


# ────────────────────────────────────────────────────────────────────
# Routes - installation lifecycle
# ────────────────────────────────────────────────────────────────────


@router.get("/{app_id}/check-update", response_model=AppResponse)
async def check_update(
    request: Request, app_id: str,
) -> AppResponse:
    """Report whether an updated version of the installed app is available.

    Only built-in apps currently support this - the wheel ships a
    possibly-newer version of each builtin and the registry tracks the
    installed hash. Hub and git sources will support this in v2.
    """
    registry = _get_registry(request)
    pkg = await registry.get(app_id)
    if pkg is None:
        raise HTTPException(
            status_code=404,
            detail=f"App '{app_id}' not installed",
        )

    flow = _build_install_flow(request)
    source = flow._sources.get(pkg["source_type"])
    if source is None:
        return AppResponse(
            success=True,
            data={
                "app_id": app_id,
                "current_version": pkg["version"],
                "latest_version": None,
                "update_available": False,
                "reason": f"unknown source type {pkg['source_type']!r}",
            },
        )

    try:
        latest = await source.check_update(
            installed_uri=pkg["source_uri"],
            current_hash=pkg["hash"],
        )
    except NotImplementedError:
        return AppResponse(
            success=True,
            data={
                "app_id": app_id,
                "current_version": pkg["version"],
                "latest_version": None,
                "update_available": False,
                "reason": (
                    f"check-update is not yet implemented for "
                    f"{pkg['source_type']!r} sources (deferred to v2)"
                ),
            },
        )
    except Exception as exc:
        logger.warning("check_update failed for %s: %s", app_id, exc)
        latest = None

    return AppResponse(
        success=True,
        data={
            "app_id": app_id,
            "current_version": pkg["version"],
            "latest_version": latest,
            "update_available": latest is not None and latest != pkg["version"],
        },
    )


@router.post("/install", response_model=AppResponse)
async def install_app(
    request: Request, body: InstallRequest,
) -> AppResponse:
    """Install a new app from one of the 4 supported sources.

    First call (without ``accept_permissions``) returns ``409`` with
    the permissions payload so the client can show a consent dialog.
    Second call (with ``accept_permissions=true``) actually installs.

    Hub and git sources return 501 in v1 - their fetch implementations
    are stubs.
    """
    if body.source_type in (SourceType.HUB, SourceType.GIT):
        raise HTTPException(
            status_code=501,
            detail=(
                f"{body.source_type!r} source is deferred to v2 "
                f"(see docs/APP_PACKAGES.md §14). "
                f"Use source_type='local' with a directory containing "
                f"package.toml + app.yaml until then."
            ),
        )

    scope = body.scope or "user"
    if scope not in ("user", "system"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid scope {scope!r} - must be 'user' or 'system'",
        )
    if scope == "system" and not _caller_is_admin(request):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only admins can install apps at scope='system'. "
                "Use scope='user' to install for yourself only."
            ),
        )

    flow = _build_install_flow(request)
    on_deploy = _resolve_deploy_callback(request)

    user_id = _caller_user_id(request)
    owner_user_id = user_id if scope == "user" else None

    try:
        result = await flow.install(
            source_type=body.source_type,
            source_uri=body.source_uri,
            installed_by=user_id,
            accept_permissions=body.accept_permissions,
            on_deploy=on_deploy,
            scope=scope,
            owner_user_id=owner_user_id,
        )
    except PermissionsRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "permissions_required",
                "app_id": exc.manifest_id,
                "permissions": exc.perms,
                "message": (
                    "Review the requested permissions and retry the "
                    "request with accept_permissions=true to proceed."
                ),
            },
        )
    except PackageIdCollision as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "app_already_installed",
                "app_id": exc.package_id,
                "existing": exc.existing,
                "message": (
                    f"App '{exc.package_id}' is already installed "
                    f"from source '{exc.existing.get('source_type')}'. "
                    f"Uninstall it first or use a different app id."
                ),
            },
        )
    except InstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("install_app failed for %s", body.source_uri)
        raise HTTPException(status_code=500, detail=str(exc))

    return AppResponse(
        success=True,
        data={
            "app_id": result.package_id,
            "version": result.version,
            "source_type": result.source_type,
            "install_dir": result.install_dir,
            "hash": result.hash,
            "deployed": result.deployed,
            "deploy_error": result.deploy_error,
            "scope": scope,
            "owner_user_id": owner_user_id,
        },
    )


@router.post("/{app_id}/upgrade", response_model=AppResponse)
async def upgrade_app(
    request: Request, app_id: str, body: UpgradeRequest,
) -> AppResponse:
    """Upgrade an installed app to a new version.

    Same permissions consent flow as install. On compile/deploy
    failure the InstallFlow rolls back to the previous version
    automatically (locked design D8).

    Supports every source type the matching ``flow.install()`` does
    (local / git / hub) - the underlying ``InstallFlow.upgrade()``
    pipeline is source-agnostic.
    """
    registry = _get_registry(request)
    caller_id = _caller_user_id(request)
    existing = await registry.resolve_for_caller(
        app_id, user_id=caller_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not visible to caller",
        )
    pkg_scope = existing.get("scope") or "system"
    pkg_owner = existing.get("owner_user_id")

    if pkg_scope == "system" and not _caller_is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="Only admins can upgrade system-scoped apps",
        )
    if pkg_scope == "user" and pkg_owner != caller_id and not _caller_is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="You can only upgrade your own user-scoped installs",
        )

    flow = _build_install_flow(request)
    on_deploy = _resolve_deploy_callback(request)

    # When the client doesn't override the source, reuse what the registry
    # recorded at install time. Lets a UI "Upgrade" click work for any
    # source kind (hub / git / local) without the client having to know.
    src_type = body.source_type or existing.get("source_type")
    src_uri = body.source_uri or existing.get("source_uri")
    if not src_type or not src_uri:
        raise HTTPException(
            status_code=400,
            detail="No source recorded for this app and none provided in the request",
        )

    try:
        result = await flow.upgrade(
            app_id,
            source_type=src_type,
            source_uri=src_uri,
            accept_permissions=body.accept_permissions,
            installed_by=caller_id,
            on_deploy=on_deploy,
            scope=pkg_scope,
            owner_user_id=pkg_owner,
        )
    except PermissionsRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "permissions_required",
                "app_id": exc.manifest_id,
                "permissions": exc.perms,
            },
        )
    except InstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("upgrade_app failed for %s", app_id)
        raise HTTPException(status_code=500, detail=str(exc))

    return AppResponse(
        success=True,
        data={
            "app_id": result.package_id,
            "version": result.version,
            "deployed": result.deployed,
            "deploy_error": result.deploy_error,
        },
    )


# NOTE: ``GET /{app_id}/assets/{asset_path:path}`` and
# ``GET /{app_id}/icon`` live in ``apps.py`` (they pre-date this
# module and handle both the deployed-bundle path and the
# package-install-dir fallback - richer than what we'd write here).
# Do NOT re-declare them here; ``apps_router`` is registered before
# ``apps_install_router`` in ``server.py`` so any duplicate would be
# dead code anyway.


@router.post("/{app_id}/uninstall", response_model=AppResponse)
async def uninstall_app(
    request: Request,
    app_id: str,
    body: UninstallRequest | None = None,
) -> AppResponse:
    """Uninstall an app - wipes disk + DB row + undeploys.

    Built-in apps refuse without ``force=true`` AND admin permission
    (locked design D9). User data (workspaces, credentials, drafts) is
    preserved.
    """
    force = body.force if body else False

    registry = _get_registry(request)
    caller_id = _caller_user_id(request)
    existing = await registry.resolve_for_caller(
        app_id, user_id=caller_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=404, detail=f"App '{app_id}' not visible",
        )
    pkg_scope = existing.get("scope") or "system"
    pkg_owner = existing.get("owner_user_id")

    if pkg_scope == "system" and not _caller_is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="Only admins can uninstall system-scoped apps",
        )
    if pkg_scope == "user" and pkg_owner != caller_id and not _caller_is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="You can only uninstall your own user-scoped installs",
        )

    flow = _build_install_flow(request)
    manager = None
    try:
        manager = _get_manager(request)
    except Exception:
        pass

    async def _on_undeploy(
        pkg_id: str,
        scope: str = "system",
        owner_user_id: str | None = None,
    ):
        if manager is None:
            return
        try:
            undeploy = getattr(manager, "undeploy", None)
            if undeploy is not None:
                import inspect
                sig = inspect.signature(undeploy)
                if "user_id" in sig.parameters:
                    await undeploy(pkg_id, user_id=owner_user_id)
                else:
                    await undeploy(pkg_id)
        except Exception as exc:
            logger.warning(
                "undeploy of %s failed during uninstall: %s",
                pkg_id, exc,
            )

    try:
        ok = await flow.uninstall(
            app_id, force=force, on_undeploy=_on_undeploy,
            scope=pkg_scope, owner_user_id=pkg_owner,
        )
    except InstallError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.exception("uninstall_app failed for %s", app_id)
        raise HTTPException(status_code=500, detail=str(exc))

    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"App '{app_id}' not found",
        )

    return AppResponse(
        success=True,
        data={"app_id": app_id, "uninstalled": True},
    )
