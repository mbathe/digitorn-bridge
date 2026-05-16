"""Daemon-side Hub install endpoint.

Browse / search / detail / reviews / reports / stats / categories now
go directly from the client to ``hub.digitorn.ai/api/v1/*``: the Hub
validates the central RS256 JWT natively (see
``digitorn_hub.auth.central``), so there's no value in proxying.

The ONE call that still belongs on the daemon is install: fetching the
package archive and atomically deploying it is intrinsically a
daemon-local operation. The handler reads the caller's central Bearer
token from the Authorization header, hands it to ``HubSource`` so the
download is authenticated as the calling user, and runs ``InstallFlow``.

Routes:
    POST /api/hub/install      install ``hub://{publisher}/{package_id}[@v]``
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from digitorn.core.api.packages import (
    _build_install_flow,
    _caller_user_id,
    _resolve_deploy_callback,
)
from digitorn.core.packages import SourceType
from digitorn.core.packages.install import (
    InstallError,
    PackageIdCollision,
    PermissionsRequired,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hub", tags=["hub"])


class HubInstallRequest(BaseModel):
    publisher: str = Field(..., min_length=1, max_length=80)
    package_id: str = Field(..., min_length=1, max_length=80)
    version: str | None = None
    # Accept ``bool`` (web client - "user clicked Accept") OR ``list[str]``
    # (Flutter, CLI - explicit list of accepted scopes) OR ``None`` (first
    # call, daemon returns 409 with the permissions block). Coerced to a
    # truthy value before flow.install which only checks bool-ness.
    accept_permissions: bool | list[str] | None = None
    scope: str = Field(default="user", pattern="^(user|system)$")


def _bearer_from(request: Request) -> str | None:
    """Lift the caller's central JWT out of the Authorization header so
    HubSource can forward it to the Hub when downloading the archive.

    Returns None when the header is missing - the Hub allows anonymous
    reads on most catalogue endpoints, but private packages (publisher
    drafts, premium content) will fail with 401 from HubSource itself.
    That's the correct surface for the user.
    """
    h = request.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        return h.split(" ", 1)[1].strip()
    return None


@router.post("/install")
async def hub_install(
    payload: HubInstallRequest,
    request: Request,
) -> dict[str, Any]:
    """Install a package from the configured Hub.

    Authentication: the user's central JWT (from the Authorization
    header) is required. ``daemon_user_id`` comes from the same JWT
    via ``request.state.user_id`` populated by the auth middleware.
    """
    daemon_user_id = _caller_user_id(request)
    if not daemon_user_id or daemon_user_id == "local":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "you must be logged in to install hub apps",
        )

    # Stash the central token on request.state so HubSource (built by
    # _build_install_flow below) can pick it up via getattr. Keeping
    # the attribute name `hub_token` so the existing source code in
    # core.api.packages._build_install_flow doesn't need to change.
    request.state.hub_token = _bearer_from(request)

    if payload.scope == "system":
        perms = getattr(request.state, "permissions", []) or []
        if not ("*" in perms or "admin" in perms or "packages.admin" in perms):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "system-scope install requires admin",
            )

    suffix = f"@{payload.version}" if payload.version else ""
    source_uri = f"hub://{payload.publisher}/{payload.package_id}{suffix}"

    flow = _build_install_flow(request)
    deploy_cb = _resolve_deploy_callback(request)

    owner = daemon_user_id if payload.scope == "user" else None
    install_kwargs = dict(
        source_type=SourceType.HUB,
        source_uri=source_uri,
        installed_by=daemon_user_id,
        accept_permissions=payload.accept_permissions,
        on_deploy=deploy_cb,
        scope=payload.scope,
        owner_user_id=owner,
    )

    try:
        result = await flow.install(**install_kwargs)
    except PermissionsRequired as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "permissions_required",
                "package_id": exc.manifest_id,
                "permissions": exc.perms,
            },
        )
    except PackageIdCollision:
        # Already installed at this scope. Treat the second click on
        # "Install" as an upgrade - same UX the user expects.
        try:
            result = await flow.upgrade(
                payload.package_id,
                source_type=SourceType.HUB,
                source_uri=source_uri,
                installed_by=daemon_user_id,
                accept_permissions=payload.accept_permissions,
                on_deploy=deploy_cb,
                scope=payload.scope,
                owner_user_id=owner,
            )
        except PermissionsRequired as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "permissions_required",
                    "package_id": exc.manifest_id,
                    "permissions": exc.perms,
                },
            )
        except InstallError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except InstallError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    from dataclasses import asdict, is_dataclass
    if is_dataclass(result):
        result_dict = asdict(result)
    elif hasattr(result, "model_dump"):
        result_dict = result.model_dump()
    elif hasattr(result, "dict"):
        result_dict = result.dict()
    else:
        result_dict = dict(result)
    return {
        "package_id": payload.package_id,
        "publisher": payload.publisher,
        "scope": payload.scope,
        "result": result_dict,
    }
