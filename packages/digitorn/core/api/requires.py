"""Digitorn — Requirements API routes.

    GET  /api/requires              List all module requirements with status
    POST /api/requires/install      Install a specific requirement
    POST /api/requires/install-all  Install all missing requirements
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/requires", tags=["requires"])


class InstallRequest(BaseModel):
    """Request body for POST /api/requires/install."""
    name: str


@router.get("")
async def list_requirements() -> dict[str, Any]:
    """List all module requirements grouped by module, with install status."""
    from digitorn.core.requirements import scan_all_requirements, _detect_available_managers

    reqs = scan_all_requirements()
    managers = _detect_available_managers()

    # Group by module
    by_module: dict[str, list[dict[str, Any]]] = {}
    for req in reqs:
        by_module.setdefault(req.module_id, []).append(req.to_dict())

    installed = sum(1 for r in reqs if r.installed)
    missing = sum(1 for r in reqs if not r.installed)

    return {
        "requirements": by_module,
        "summary": {
            "total": len(reqs),
            "installed": installed,
            "missing": missing,
        },
        "available_managers": list(managers.keys()),
    }


@router.post("/install")
async def install_requirement(body: InstallRequest) -> dict[str, Any]:
    """Install a specific requirement by name."""
    from digitorn.core.requirements import scan_all_requirements, install_requirement as _install

    reqs = scan_all_requirements()
    target = next((r for r in reqs if r.name == body.name or r.binary == body.name), None)

    if target is None:
        raise HTTPException(status_code=404, detail=f"Requirement '{body.name}' not found")

    if target.installed:
        return {
            "success": True,
            "name": target.name,
            "already_installed": True,
            "path": target.path,
        }

    return await _install(target)


@router.post("/install-all")
async def install_all_missing() -> dict[str, Any]:
    """Install all missing requirements."""
    from digitorn.core.requirements import install_all_missing as _install_all

    results = await _install_all()

    succeeded = sum(1 for r in results if r.get("success"))
    failed = sum(1 for r in results if not r.get("success"))

    return {
        "results": results,
        "summary": {
            "attempted": len(results),
            "succeeded": succeeded,
            "failed": failed,
        },
    }
