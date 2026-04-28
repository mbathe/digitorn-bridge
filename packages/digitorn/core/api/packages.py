"""Shared helpers + request models for the app installation lifecycle.

HISTORY: this module USED to expose ``/api/packages/*`` HTTP routes.
On 2026-04-21 the routes were physically moved under ``/api/apps/*``
(see ``api/apps_install.py``) so the Flutter client has a single
mental model (``app``) instead of juggling both ``package`` and
``app`` entities.

The module stays as a **library layer** - other code paths (built-in
bootstrap, CLI installers, admin scripts, tests) still need the
helpers and Pydantic bodies. Importing from here keeps those callers
stable even as the HTTP surface evolves.

What lives here:

- ``_get_registry(request)`` - pull the singleton ``PackageRegistry``.
- ``_build_install_flow(request)`` - assemble an ``InstallFlow`` with
  all four sources wired (builtin/local/hub/git).
- ``_resolve_deploy_callback(request)`` - build an optional ``on_deploy``
  callback that hands the package's ``app.yaml`` to the live
  ``AppManager`` so ``install/upgrade`` can deploy in the same call.
- ``_caller_user_id`` / ``_caller_is_admin`` - auth helpers.
- ``InstallRequest`` / ``UpgradeRequest`` / ``UninstallRequest`` -
  Pydantic request bodies (kept stable; Flutter + HTTP handlers in
  ``apps_install.py`` depend on them).

What is NOT here anymore:

- ``router = APIRouter(...)`` - deleted. HTTP routes live under
  ``/api/apps/*`` in ``apps_install.py``.
- Route handlers (``list_packages``, ``install_package``, …) - ditto.

If you're looking for the HTTP routes, see ``api/apps_install.py``.
If you're writing non-HTTP code that needs to drive an install
(CLI tool, bootstrap, tests), import from here.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from digitorn.core.packages import (
    InstallFlow,
    PackageRegistry,
    SourceType,
)
from digitorn.core.packages.bootstrap import DEFAULT_INSTALL_ROOT
from digitorn.core.packages.sources.builtin import BuiltinSource
from digitorn.core.packages.sources.git import GitSource
from digitorn.core.packages.sources.hub import HubSource
from digitorn.core.packages.sources.local import LocalSource

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Registry / flow helpers
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
                "set up the app lifecycle system at startup - check the "
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
    other 3 sources via the HTTP route.

    Hub and git sources are stubs that raise NotImplementedError -
    ``InstallFlow`` translates those into ``InstallError`` which the
    HTTP layer surfaces as 501.
    """
    from digitorn.core.config import get_settings
    from digitorn.core.packages.bootstrap import _default_builtins_dir

    registry = _get_registry(request)
    hub_cfg = get_settings().hub
    sources: dict[str, Any] = {
        SourceType.BUILTIN: BuiltinSource(_default_builtins_dir()),
        SourceType.LOCAL: LocalSource(link_mode="copy"),
        SourceType.HUB: HubSource(
            hub_url=hub_cfg.url,
            verify_ssl=hub_cfg.verify_ssl,
            timeout_seconds=hub_cfg.timeout_seconds,
            auth_token=getattr(request.state, "hub_token", None),
        ),
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
    ``AppManager`` so the package becomes a deployed app in the same
    operation. If the manager isn't accessible (early lifespan,
    test harness without HTTP state), we just return ``None`` and
    the install completes without deploying - the caller deploys
    later via the existing apps API.

    The callback uses the 4-arg signature ``(yaml_path, package_id,
    scope, owner_user_id)`` so the deploy lands under the SAME key
    (``system:<id>`` or ``user:<uid>:<id>``) that uninstall will later
    pop. The 2-arg legacy form would deploy under ``system:<id>``
    regardless of the install scope, and a subsequent user-scope
    uninstall would miss the key - leaving the app wedged in memory
    even after its registry row + disk are gone.
    """
    try:
        # Late import to avoid cycles when ``apps.py`` imports this module.
        from digitorn.core.api.apps_v2 import _get_manager
        manager = _get_manager(request)
    except Exception:
        return None

    async def _on_deploy(yaml_path, package_id, scope="system",
                         owner_user_id=None):
        return await manager.deploy(
            yaml_path,
            force=True,
            scope=scope,
            owner_user_id=owner_user_id,
        )

    return _on_deploy


# ────────────────────────────────────────────────────────────────────
# Auth helpers
# ────────────────────────────────────────────────────────────────────


def _caller_user_id(request: Request) -> str:
    """Pull the authenticated caller's user_id (or 'local' in dev)."""
    return getattr(request.state, "user_id", None) or "local"


def _caller_is_admin(request: Request) -> bool:
    """True when the caller has admin perms (``*``, ``admin``, or ``packages.admin``)."""
    perms = getattr(request.state, "permissions", []) or []
    return "*" in perms or "admin" in perms or "packages.admin" in perms


# ────────────────────────────────────────────────────────────────────
# Pydantic request bodies
# ────────────────────────────────────────────────────────────────────
#
# These are the stable contract the Flutter client POSTs. The HTTP
# routes in ``api/apps_install.py`` import them unchanged. Any change
# here is a breaking API change for existing clients - bump carefully.


class InstallRequest(BaseModel):
    """``POST /api/apps/install`` body.

    BUG-100 back-compat: older SDKs and the original README example
    post ``{source, force}`` instead of ``{source_type, source_uri}``.
    The ``_expand_source`` validator accepts either shape and fills in
    the missing parts so both forms keep working.
    """

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


class UpgradeRequest(BaseModel):
    """``POST /api/apps/{app_id}/upgrade`` body.

    Both ``source_type`` and ``source_uri`` are optional; when omitted the
    handler reuses the values stored in the install registry, so a UI
    "Upgrade" click against a hub-installed app no longer needs to
    re-specify them.
    """

    source_type: str | None = Field(
        default=None,
        description="Override the original source. Falls back to the registry value.",
    )
    source_uri: str | None = Field(
        default=None,
        description="Override the fetch URI. Falls back to the registry value.",
    )
    accept_permissions: bool = Field(default=False)

    @model_validator(mode="after")
    def _reject_blank(self) -> "UpgradeRequest":
        if self.source_uri is not None and not self.source_uri.strip():
            raise ValueError("source_uri must not be blank")
        return self


class UninstallRequest(BaseModel):
    """``POST /api/apps/{app_id}/uninstall`` body."""

    force: bool = Field(
        default=False,
        description=(
            "Required for built-ins (locked design D9). Without it, "
            "uninstall of a builtin returns 403."
        ),
    )


__all__ = [
    # helpers
    "_get_registry",
    "_build_install_flow",
    "_resolve_deploy_callback",
    "_caller_user_id",
    "_caller_is_admin",
    # request models
    "InstallRequest",
    "UpgradeRequest",
    "UninstallRequest",
]
