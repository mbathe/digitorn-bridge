"""Digitorn - Configuration API routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ValidationError

from digitorn.core.config import Settings

router = APIRouter(prefix="/api/config", tags=["config"])

RESTART_REQUIRED = frozenset({
    "server.host",
    "server.port",
    "server.workers",
    "database.url",
    "database.pool_size",
})


class ConfigPatchRequest(BaseModel):
    """Partial config update. Only provided fields are changed."""

    server: dict[str, Any] | None = None
    database: dict[str, Any] | None = None
    modules: dict[str, Any] | None = None
    logging: dict[str, Any] | None = None


class ConfigPatchResponse(BaseModel):
    applied: dict[str, Any]
    restart_required: list[str]
    errors: list[str]


def _flatten_keys(data: dict[str, Any], prefix: str = "") -> list[str]:
    keys = []
    for k, v in data.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys.extend(_flatten_keys(v, full))
        else:
            keys.append(full)
    return keys


@router.get("")
async def get_config(request: Request) -> dict[str, Any]:
    """Return the current runtime configuration."""
    settings: Settings = request.app.state.settings
    return settings.model_dump(mode="json")


@router.patch("")
async def patch_config(
    body: ConfigPatchRequest,
    request: Request,
) -> ConfigPatchResponse:
    """Apply partial configuration changes at runtime."""
    perms = list(getattr(request.state, "permissions", []) or [])
    roles = list(getattr(request.state, "roles", []) or [])
    is_admin = "*" in perms or "admin" in roles or "*" in roles
    if not is_admin:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(
            status_code=403,
            detail="PATCH /api/config requires admin role.",
        )

    settings: Settings = request.app.state.settings
    patch = body.model_dump(exclude_none=True)

    if not patch:
        return ConfigPatchResponse(applied={}, restart_required=[], errors=[])

    changed_keys = _flatten_keys(patch)
    needs_restart = [k for k in changed_keys if k in RESTART_REQUIRED]

    current = settings.model_dump()
    for section, values in patch.items():
        if section in current and isinstance(values, dict):
            current[section].update(values)
        else:
            current[section] = values

    errors: list[str] = []
    try:
        validated = Settings(**current)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            errors.append(f"{loc}: {err['msg']}")
        return ConfigPatchResponse(applied={}, restart_required=[], errors=errors)

    from digitorn.core.config import override_settings

    override_settings(validated)
    request.app.state.settings = validated

    return ConfigPatchResponse(
        applied=patch,
        restart_required=needs_restart,
        errors=[],
    )


@router.get("/browse")
async def browse_directories(
    path: str = Query(default="~", description="Directory to list"),
) -> dict[str, Any]:
    """List subdirectories at a given path for workspace selection."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        return {"path": str(resolved), "dirs": [], "error": "Not a directory"}

    dirs: list[dict[str, str]] = []
    try:
        for entry in sorted(resolved.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        return {"path": str(resolved), "dirs": [], "error": "Permission denied"}

    parent = str(resolved.parent) if resolved.parent != resolved else None
    return {"path": str(resolved), "parent": parent, "dirs": dirs}
