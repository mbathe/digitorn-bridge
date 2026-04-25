"""HTTP routes for the AppPackages system.

Six routes covering the full lifecycle of an installed package:

    GET    /api/packages                       — list installed
    GET    /api/packages/{id}                  — get one (with drift check)
    POST   /api/packages/install               — install (with permissions probe)
    POST   /api/packages/{id}/upgrade          — upgrade
    POST   /api/packages/{id}/uninstall        — uninstall
    GET    /api/packages/{id}/check-update     — content drift report

Locked design references:

- D5 — permissions consent flow (409 with payload, then re-call with accept_permissions=true)
- D9 — built-in uninstall protection (admin + force)
- D11 — install permission gating (every mutation requires package.install)
- D12 — id collision is a strict refusal (409, no merge)

Hub and git source paths return 501 with a clear message because
their concrete implementations are deferred to v2 (per §14 of the
design doc).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from digitorn.core.api.apps import AppResponse, _get_manager
from digitorn.core.packages import (
    InstallError,
    InstallFlow,
    PackageIdCollision,
    PackageRegistry,
    PermissionsRequired,
    SourceType,
    Status,
    require_install_permission,
)
from digitorn.core.packages.bootstrap import DEFAULT_INSTALL_ROOT
from digitorn.core.packages.sources.builtin import BuiltinSource
from digitorn.core.packages.sources.git import GitSource
from digitorn.core.packages.sources.hub import HubSource
from digitorn.core.packages.sources.local import LocalSource

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packages", tags=["packages"])


# ────────────────────────────────────────────────────────────────────
# Dependencies
# ────────────────────────────────────────────────────────────────────


def _get_registry(request: Request) -> PackageRegistry:
    """Pull the singleton ``PackageRegistry`` off ``app.state``.

    Initialised in the lifespan after the credential store. If the
    bootstrap path failed for any reason (no DB, schema mismatch),
    we surface a 503 with a clear message instead of a 500 stack
    trace deeper in the call chain.
    """
    registry = getattr(request.app.state, "package_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Package registry not initialized. The daemon couldn't "
                "set up the AppPackages system at startup — check the "
                "logs for the underlying cause."
            ),
        )
    return registry


def _build_install_flow(request: Request) -> InstallFlow:
    """Construct an InstallFlow with all 4 sources mapped.

    BuiltinSource is wired against the wheel's ``packages/digitorn/builtins/``
    directory so the route can technically install a builtin from
    its source URI. In practice the bootstrap loop installs all
    builtins automatically and users only ever install from the
    other 3 sources via this route.

    Hub and git sources are stubs that raise NotImplementedError —
    InstallFlow translates those into ``InstallError`` which the
    route layer surfaces as 501.
    """
    from digitorn.core.packages.bootstrap import _default_builtins_dir

    registry = _get_registry(request)
    sources: dict[str, Any] = {
        SourceType.BUILTIN: BuiltinSource(_default_builtins_dir()),
        SourceType.LOCAL: LocalSource(link_mode="copy"),
        SourceType.HUB: HubSource(),
        SourceType.GIT: GitSource(),
    }
    return InstallFlow(
        registry=registry,
        source_map=sources,
        install_root=DEFAULT_INSTALL_ROOT,
    )


def _resolve_deploy_callback(request: Request):
    """Return an optional ``on_deploy`` callback for the install flow.

    The callback hands the package's ``app.yaml`` to the live
    ``AppManager`` so the package becomes a deployed app. If the
    manager isn't accessible (e.g. early in the lifespan), we just
    return None and the install completes without deploying — the
    user can deploy later via the existing apps API.
    """
    try:
        manager = _get_manager(request)
    except Exception:
        return None

    async def _on_deploy(yaml_path, package_id):
        return await manager.deploy(yaml_path, force=True)

    return _on_deploy


# ────────────────────────────────────────────────────────────────────
# Request bodies
# ────────────────────────────────────────────────────────────────────


class InstallRequest(BaseModel):
    """``POST /api/packages/install`` body."""

    # BUG-100: older SDKs (and the README example) post
    # ``{source, force}`` instead of ``{source_type, source_uri}``.
    # Accept the collapsed form and split it on the way in so both
    # shapes work. The ``source`` + ``force`` fields stay optional so
    # new callers can keep using the explicit pair.
    model_config = {"populate_by_name": True, "extra": "allow"}

    source_type: str = Field(
        default="",
        description="One of: builtin, local, hub, git. Inferred from ``source`` when absent.",
    )
    source_uri: str = Field(
        default="",
        description=(
            "Source-specific URI. ``local``: a filesystem path. "
            "``hub``: ``hub://publisher/name@version`` (v2). "
            "``git``: ``git+https://...`` (v2). ``builtin``: ``bundle://digitorn/<id>``."
        ),
    )
    source: str | None = Field(
        default=None,
        description=(
            "Convenience alias: a single string the server splits into "
            "(source_type, source_uri). Example: ``bundle://digitorn/chat``, "
            "``hub://user/app@1``, ``git+https://github.com/...``, a plain "
            "filesystem path, or ``digitorn-chat`` (resolved as builtin)."
        ),
    )
    force: bool = Field(
        default=False,
        description="Legacy alias for ``accept_permissions``; when true, skip the permissions probe.",
    )

    @model_validator(mode="after")
    def _expand_source(self) -> "InstallRequest":
        # If caller only gave ``source``, split into type + uri so the
        # rest of the pipeline keeps its explicit contract.
        if (not self.source_type or not self.source_uri) and self.source:
            s = self.source.strip()
            if s.startswith("bundle://"):
                self.source_type = self.source_type or "builtin"
                self.source_uri = self.source_uri or s
            elif s.startswith("hub://"):
                self.source_type = self.source_type or "hub"
                self.source_uri = self.source_uri or s
            elif s.startswith(("git+", "https://github.com/", "git@")):
                self.source_type = self.source_type or "git"
                self.source_uri = self.source_uri or s
            elif "/" in s or "\\" in s or s.startswith("."):
                self.source_type = self.source_type or "local"
                self.source_uri = self.source_uri or s
            else:
                # Bare ID: assume a builtin bundle (``digitorn-chat``).
                self.source_type = self.source_type or "builtin"
                self.source_uri = self.source_uri or f"bundle://digitorn/{s}"
        if self.force and not self.accept_permissions:
            # Back-compat: ``force=true`` used to imply permissions
            # pre-accepted on older SDKs.
            object.__setattr__(self, "accept_permissions", True)
        if not self.source_type or not self.source_uri:
            raise ValueError(
                "Provide either {source_type + source_uri} or {source}."
            )
        return self
    accept_permissions: bool = Field(
        default=False,
        description=(
            "Set to true to bypass the permissions probe. The first call "
            "without it returns 409 with a permissions payload that the "
            "client shows in a confirmation dialog before retrying."
        ),
    )
    link_mode: str = Field(
        default="copy",
        description=(
            "For local sources: 'copy' (default) makes an independent "
            "copy, 'symlink' creates a symlink to the source directory "
            "for in-place dev iteration."
        ),
    )
    scope: str = Field(
        default="user",
        description=(
            "Install visibility: 'user' (personal, invisible to others) "
            "or 'system' (shared across all users, admin-only). "
            "Non-admin callers are limited to 'user'."
        ),
    )


class UpgradeRequest(BaseModel):
    """``POST /api/packages/{id}/upgrade`` body."""

    source_type: str = Field(
        default="local",
        description="Override the original source. Defaults to local.",
    )
    source_uri: str = Field(
        ...,
        description="Where to fetch the new version from.",
    )
    accept_permissions: bool = Field(default=False)


class UninstallRequest(BaseModel):
    """``POST /api/packages/{id}/uninstall`` body."""

    force: bool = Field(
        default=False,
        description=(
            "Required for built-ins (locked design D9). Without it, "
            "uninstall of a builtin returns 403."
        ),
    )


# ────────────────────────────────────────────────────────────────────
# Routes — read
# ────────────────────────────────────────────────────────────────────


def _caller_user_id(request: Request) -> str:
    """Pull the authenticated caller's user_id (or 'local' in dev)."""
    return getattr(request.state, "user_id", None) or "local"


def _caller_is_admin(request: Request) -> bool:
    """True when the caller has admin perms (`*` or `admin`)."""
    perms = getattr(request.state, "permissions", []) or []
    return "*" in perms or "admin" in perms or "packages.admin" in perms


@router.get("", response_model=AppResponse)
async def list_packages(
    request: Request,
    source_type: str | None = None,
    status: str | None = None,
    all: bool = False,
) -> AppResponse:
    """List packages visible to the caller.

    A regular user sees: their own user-scoped installs + every
    system-scoped install that isn't shadowed by a user install of
    the same ``package_id``. User packages belonging to OTHER
    users are never returned.

    Admins can pass ``?all=true`` to list every install across
    every user — useful for a management dashboard.
    """
    registry = _get_registry(request)
    user_id = _caller_user_id(request)

    if all and _caller_is_admin(request):
        rows = await registry.list_all(
            source_type=source_type, status=status,
        )
    else:
        rows = await registry.list_visible_to_user(
            user_id=user_id,
            source_type=source_type,
            status=status,
        )
    return AppResponse(
        success=True,
        data={"packages": rows, "count": len(rows)},
    )


@router.get("/{package_id}", response_model=AppResponse)
async def get_package(
    request: Request, package_id: str,
) -> AppResponse:
    """Get the package install the caller sees for ``package_id``.

    Resolution: the caller's own user-scoped install wins over a
    system install with the same id (shadow pattern). Users never
    see packages belonging to other users. 404 when there's no
    visible install.
    """
    registry = _get_registry(request)
    user_id = _caller_user_id(request)
    pkg = await registry.resolve_for_caller(package_id, user_id=user_id)
    if pkg is None:
        raise HTTPException(
            status_code=404,
            detail=f"Package '{package_id}' not installed",
        )
    try:
        drift = await registry.check_drift(package_id)
    except Exception as exc:
        logger.warning("drift check failed for %s: %s", package_id, exc)
        drift = {"error": str(exc)}
    return AppResponse(
        success=True,
        data={**pkg, "drift": drift},
    )


@router.get("/{package_id}/check-update", response_model=AppResponse)
async def check_update(
    request: Request, package_id: str,
) -> AppResponse:
    """Report whether an updated version of the package is available.

    Only built-in packages currently support this — the wheel ships
    a possibly-newer version of each builtin and the registry tracks
    the installed hash. Hub and git sources will support this in v2.
    """
    registry = _get_registry(request)
    pkg = await registry.get(package_id)
    if pkg is None:
        raise HTTPException(
            status_code=404,
            detail=f"Package '{package_id}' not installed",
        )

    flow = _build_install_flow(request)
    source = flow._sources.get(pkg["source_type"])
    if source is None:
        return AppResponse(
            success=True,
            data={
                "package_id": package_id,
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
                "package_id": package_id,
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
        logger.warning("check_update failed for %s: %s", package_id, exc)
        latest = None

    return AppResponse(
        success=True,
        data={
            "package_id": package_id,
            "current_version": pkg["version"],
            "latest_version": latest,
            "update_available": latest is not None and latest != pkg["version"],
        },
    )


# ────────────────────────────────────────────────────────────────────
# Routes — write (require package.install permission)
# ────────────────────────────────────────────────────────────────────


@router.post("/install", response_model=AppResponse)
async def install_package(
    request: Request, body: InstallRequest,
) -> AppResponse:
    """Install a new package.

    First call (without ``accept_permissions``) returns ``409`` with
    the permissions payload so the client can show a consent dialog.
    Second call (with ``accept_permissions=true``) actually installs.

    Hub and git sources return 501 — their fetch implementations
    are stubs in v1.
    """
    # Scope-based permission model: every authenticated user can
    # install packages for themselves (scope=user). Only admins
    # can install system-wide (scope=system). The old blanket
    # require_install_permission() check is gone — it was the
    # pre-scoping "install is admin-only" behavior.

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
            detail=f"Invalid scope {scope!r} — must be 'user' or 'system'",
        )
    if scope == "system" and not _caller_is_admin(request):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only admins can install packages at scope='system'. "
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
        # Locked design D5: surface the perms payload so the client
        # can show a confirmation dialog and then re-call with
        # accept_permissions=true.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "permissions_required",
                "package_id": exc.manifest_id,
                "permissions": exc.perms,
                "message": (
                    "Review the requested permissions and retry the "
                    "request with accept_permissions=true to proceed."
                ),
            },
        )
    except PackageIdCollision as exc:
        # Locked design D12: strict refusal, no merge
        raise HTTPException(
            status_code=409,
            detail={
                "error": "package_already_installed",
                "package_id": exc.package_id,
                "existing": exc.existing,
                "message": (
                    f"Package '{exc.package_id}' is already installed "
                    f"from source '{exc.existing.get('source_type')}'. "
                    f"Uninstall it first or use a different package id."
                ),
            },
        )
    except InstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("install_package failed for %s", body.source_uri)
        raise HTTPException(status_code=500, detail=str(exc))

    return AppResponse(
        success=True,
        data={
            "package_id": result.package_id,
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


@router.post("/{package_id}/upgrade", response_model=AppResponse)
async def upgrade_package(
    request: Request, package_id: str, body: UpgradeRequest,
) -> AppResponse:
    """Upgrade an installed package to a new version.

    Same permissions consent flow as install. On compile/deploy
    failure the InstallFlow rolls back to the previous version
    automatically (locked design D8).
    """
    # Ownership check is done below (after resolving the existing
    # install). The old require_install_permission() blanket
    # gate is gone — users can upgrade their own user-scoped
    # installs; admins can upgrade system ones.

    if body.source_type in (SourceType.HUB, SourceType.GIT):
        raise HTTPException(
            status_code=501,
            detail=f"{body.source_type!r} upgrade is deferred to v2",
        )

    # Scope-aware resolve: find the install the caller owns.
    # Users upgrade their own user-scoped install; admins can
    # also upgrade system-scoped installs (resolved below).
    registry = _get_registry(request)
    caller_id = _caller_user_id(request)
    existing = await registry.resolve_for_caller(
        package_id, user_id=caller_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=404, detail=f"Package '{package_id}' not visible to caller",
        )
    pkg_scope = existing.get("scope") or "system"
    pkg_owner = existing.get("owner_user_id")

    # Non-admin users can only upgrade their own user-scoped installs
    if pkg_scope == "system" and not _caller_is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="Only admins can upgrade system-scoped packages",
        )
    if pkg_scope == "user" and pkg_owner != caller_id and not _caller_is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="You can only upgrade your own user-scoped installs",
        )

    flow = _build_install_flow(request)
    on_deploy = _resolve_deploy_callback(request)
    user_id = caller_id

    try:
        result = await flow.upgrade(
            package_id,
            source_type=body.source_type,
            source_uri=body.source_uri,
            accept_permissions=body.accept_permissions,
            installed_by=user_id,
            on_deploy=on_deploy,
            scope=pkg_scope,
            owner_user_id=pkg_owner,
        )
    except PermissionsRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "permissions_required",
                "package_id": exc.manifest_id,
                "permissions": exc.perms,
            },
        )
    except InstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("upgrade_package failed for %s", package_id)
        raise HTTPException(status_code=500, detail=str(exc))

    return AppResponse(
        success=True,
        data={
            "package_id": result.package_id,
            "version": result.version,
            "deployed": result.deployed,
            "deploy_error": result.deploy_error,
        },
    )


@router.get("/{package_id}/assets/{asset_path:path}")
async def get_package_asset(
    request: Request, package_id: str, asset_path: str,
):
    """Serve any file from an installed package's directory.

    Use this to fetch README.md, CHANGELOG.md, skill files
    (``skills/commit.md``), images referenced by ``assets/``,
    or any other companion file declared in the package dir.

    Guarded against path traversal — the resolved path must stay
    inside the package's install directory. Hidden files under
    ``.digitorn/`` are explicitly denied (they're daemon-managed
    state, not author content).

    This is the "generic" counterpart of ``/api/packages/{id}/icon``
    — that one reads the manifest's ``icon`` field and serves it;
    this one takes an explicit relative path.
    """
    from pathlib import Path
    from fastapi.responses import FileResponse

    registry = _get_registry(request)
    pkg = await registry.get(package_id)
    if pkg is None:
        raise HTTPException(
            status_code=404, detail=f"Package '{package_id}' not installed",
        )
    install_dir = Path(pkg.get("install_dir") or "").resolve()
    if not install_dir.is_dir():
        raise HTTPException(
            status_code=404, detail="Package install dir missing",
        )

    # Reject obvious attempts to traverse the daemon-managed area
    if asset_path.startswith(".digitorn") or "/.digitorn/" in asset_path:
        raise HTTPException(
            status_code=403, detail="Access to daemon-managed files denied",
        )

    target = (install_dir / asset_path).resolve()
    try:
        target.relative_to(install_dir)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Asset path escapes package dir",
        )
    if not target.is_file():
        raise HTTPException(
            status_code=404, detail=f"Asset not found: {asset_path}",
        )
    return FileResponse(str(target))


@router.get("/{package_id}/icon")
async def get_package_icon(request: Request, package_id: str):
    """Stream the icon file declared by a package's manifest.

    The manifest's ``package.icon`` field is a path relative to the
    package's install directory. This route resolves it to a real
    file and streams it with proper Content-Type detection.

    Falls back to 404 when the package has no icon, the field
    points outside the package dir (path traversal guard), or the
    file doesn't exist on disk.
    """
    from pathlib import Path
    from fastapi.responses import FileResponse

    registry = _get_registry(request)
    pkg = await registry.get(package_id)
    if pkg is None:
        raise HTTPException(
            status_code=404, detail=f"Package '{package_id}' not installed",
        )

    manifest = pkg.get("manifest") or {}
    package_meta = manifest.get("package") or {}
    icon_rel = package_meta.get("icon") or ""
    if not icon_rel:
        raise HTTPException(
            status_code=404, detail="Package has no icon declared",
        )

    install_dir = Path(pkg.get("install_dir") or "").resolve()
    if not install_dir.is_dir():
        raise HTTPException(
            status_code=404, detail="Package install dir missing",
        )

    # Resolve relative path and guard against traversal outside the dir
    icon_path = (install_dir / icon_rel).resolve()
    try:
        icon_path.relative_to(install_dir)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="Icon path escapes package dir",
        )
    if not icon_path.is_file():
        raise HTTPException(status_code=404, detail="Icon file not found")

    return FileResponse(str(icon_path))


@router.post("/{package_id}/uninstall", response_model=AppResponse)
async def uninstall_package(
    request: Request,
    package_id: str,
    body: UninstallRequest | None = None,
) -> AppResponse:
    """Uninstall a package.

    Built-in packages refuse without ``force=true`` AND admin
    permission (locked design D9). The package files on disk are
    wiped, the deployed app is undeployed, the registry row is
    deleted. User data (workspaces, credentials, drafts) is
    preserved.
    """
    # Old require_install_permission() blanket check is gone.
    # The ownership check below handles permissions properly:
    # users can uninstall their own user-scoped installs;
    # admins can also uninstall system-scoped installs.

    force = body.force if body else False

    # Scope-aware uninstall: resolve which install the caller
    # wants to remove. Admin can uninstall anything they own or
    # any system package; regular user can only uninstall their
    # own user-scoped installs.
    registry = _get_registry(request)
    caller_id = _caller_user_id(request)
    existing = await registry.resolve_for_caller(
        package_id, user_id=caller_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=404, detail=f"Package '{package_id}' not visible",
        )
    pkg_scope = existing.get("scope") or "system"
    pkg_owner = existing.get("owner_user_id")

    if pkg_scope == "system" and not _caller_is_admin(request):
        raise HTTPException(
            status_code=403,
            detail="Only admins can uninstall system-scoped packages",
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
                # New signature: (app_id, user_id)
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
            package_id, force=force, on_undeploy=_on_undeploy,
            scope=pkg_scope, owner_user_id=pkg_owner,
        )
    except InstallError as exc:
        # Built-in protection or other refusal
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        logger.exception("uninstall_package failed for %s", package_id)
        raise HTTPException(status_code=500, detail=str(exc))

    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Package '{package_id}' not found",
        )

    return AppResponse(
        success=True,
        data={"package_id": package_id, "uninstalled": True},
    )
