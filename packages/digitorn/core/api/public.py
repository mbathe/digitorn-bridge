"""Public, anonymous-friendly read-only views.

Whitelisted in ``server.py`` (added to ``RemoteAuthMiddleware`` allow
list), so these endpoints serve a 200 without a Bearer token. Every
route here MUST be:

  - read-only,
  - scoped to system-wide installs only (no per-user data),
  - return the minimum fields needed for a public landing page.

Today: a single endpoint listing deployed apps with ``scope=system``
so the marketing / landing surfaces can showcase the catalogue
before the visitor signs in.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from digitorn.core.api.apps_v2._shared import (
    _get_deployed,
    _get_manager,
    _validate_id,
    AppResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/public", tags=["public"])


@router.get("/apps", response_model=AppResponse)
async def list_public_apps(request: Request) -> AppResponse:
    """Anonymous-friendly view of deployed system apps.

    Returns every running app whose install scope is ``system`` (i.e.
    available to every visitor of this daemon). Per-user apps and
    disabled / broken / not-deployed installs are filtered out - the
    surface is meant for the public landing, not the management hub.

    The payload mirrors the per-entry shape of ``GET /api/apps`` so
    the same client parser works for both authenticated and anonymous
    callers.
    """
    manager = _get_manager(request)
    # `user_id=None` returns every deployed entry; we then filter to
    # the system-scoped subset so we never leak a per-user deploy
    # name through the public endpoint.
    apps = list(manager.list_apps(user_id=None))

    # ``list_apps(user_id=None)`` returns every deploy across every
    # scope, so the same app_id can appear twice (one system deploy
    # and one user deploy of the same package). De-dupe by app_id;
    # we want a single public-facing card per agent.
    public_apps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in apps:
        if not isinstance(a, dict):
            continue
        scope = (a.get("scope") or "system").lower()
        if scope != "system":
            continue
        app_id = a.get("app_id") or ""
        if not app_id or app_id in seen:
            continue
        seen.add(app_id)
        a.setdefault("runtime_status", "running")
        a.setdefault("install_status", "installed")
        public_apps.append(a)

    return AppResponse(success=True, data=public_apps)


@router.get("/apps/{app_id}/manifest", response_model=AppResponse)
async def get_public_app_manifest(request: Request, app_id: str) -> AppResponse:
    """Anonymous-friendly manifest for a single system-scoped app.

    The manifest holds the public-facing metadata the UI shows on the
    chat empty state (name, icon, greeting, quick prompts, etc.). It
    is intentionally non-sensitive - the same payload is already
    inlined in the bundle the daemon serves at ``/web-static/``.
    Returns 404 for user-scoped or undeployed apps so anonymous
    visitors can never enumerate per-user installs through this path.
    """
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail="App not deployed")
    scope = (getattr(deployed, "scope", "system") or "system").lower()
    if scope != "system":
        raise HTTPException(status_code=404, detail="App not public")
    return AppResponse(success=True, data=deployed.summary())
