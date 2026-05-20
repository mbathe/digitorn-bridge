"""Public, anonymous-friendly read-only views."""

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
    """Anonymous-friendly view of deployed system apps."""
    manager = _get_manager(request)
    apps = list(manager.list_apps(user_id=None))

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

    if public_apps:
        try:
            from sqlalchemy import select as _select
            from digitorn.core.database import get_session_factory as _gsf
            from digitorn.core.models import Application as _App
            _sf = _gsf()
            _app_ids = [a.get("app_id") or "" for a in public_apps]
            _app_ids = list({a for a in _app_ids if a})
            if _app_ids:
                async with _sf() as _s:
                    _stmt = _select(_App.app_id).where(
                        _App.app_id.in_(_app_ids),
                    ).where(_App.scope == "system").where(_App.hidden == True)  # noqa: E712
                    _r = await _s.execute(_stmt)
                    _hidden_ids = {row.app_id for row in _r.all()}
                if _hidden_ids:
                    public_apps = [
                        a for a in public_apps
                        if (a.get("app_id") or "") not in _hidden_ids
                    ]
        except Exception:
            pass  # defensive: don't break the public listing on filter failure

    return AppResponse(success=True, data=public_apps)


@router.get("/apps/{app_id}/manifest", response_model=AppResponse)
async def get_public_app_manifest(request: Request, app_id: str) -> AppResponse:
    """Anonymous-friendly manifest for a single system-scoped app."""
    _validate_id(app_id)
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail="App not deployed")
    scope = (getattr(deployed, "scope", "system") or "system").lower()
    if scope != "system":
        raise HTTPException(status_code=404, detail="App not public")
    return AppResponse(success=True, data=deployed.summary())
