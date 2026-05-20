"""User skill routes - CRUD on the per-user, per-app skill library"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ._shared import (
    AppResponse,
    _get_deployed,
    _raise_not_deployed,
    _validate_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["apps"])


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class SkillCreateRequest(BaseModel):
    """POST body for creating a user skill."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=300)
    instructions: str = Field(..., min_length=1)


class SkillUpdateRequest(BaseModel):
    """PATCH body. Only provided fields are written; `model_fields_set`"""

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=300)
    instructions: str | None = Field(default=None, min_length=1)


def _caller_user_id(request: Request) -> str:
    """Like the shared helper but raises 401 instead of returning"""
    uid = getattr(request.state, "user_id", None)
    if not uid or uid in ("anonymous", "system"):
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(uid)


def _ensure_deployed(request: Request, app_id: str):
    deployed = _get_deployed(request, app_id)
    if deployed is None:
        _raise_not_deployed(request, app_id)
    return deployed


def _allow_user_skills(deployed: Any) -> bool:
    compiled = getattr(deployed, "compiled", None)
    if compiled is None:
        return False
    return bool(getattr(compiled, "allow_user_skills", False))


def _app_skills(deployed: Any) -> list[dict[str, str]]:
    """Materialise the app-declared skills (no content). The compiled"""
    compiled = getattr(deployed, "compiled", None)
    raw = list(getattr(compiled, "skills", []) or []) if compiled else []
    return [
        {
            "command": s.get("command", ""),
            "description": s.get("description", ""),
        }
        for s in raw
        if s.get("command")
    ]


def _validate_name(name: str) -> str:
    """Normalise + validate the slug"""
    canonical = name.strip().lower()
    if not _NAME_RE.match(canonical):
        raise HTTPException(
            status_code=422,
            detail=(
                "Invalid skill name. Must be 1..64 chars, lowercase "
                "letters/digits/hyphens, starting with a letter or digit."
            ),
        )
    return canonical


def _serialize(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "app_id": row.app_id,
        "name": row.name,
        "description": row.description or "",
        "instructions": row.instructions,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _require_user_skills(deployed: Any) -> None:
    if not _allow_user_skills(deployed):
        raise HTTPException(
            status_code=403,
            detail=(
                "This app does not allow user-authored skills. The app "
                "developer must set `dev.allow_user_skills: true` in "
                "the YAML."
            ),
        )


@router.get("/{app_id}/skills", response_model=AppResponse)
async def list_skills(request: Request, app_id: str) -> AppResponse:
    """List the skills available for this app: the YAML-declared"""
    _validate_id(app_id)
    user_id = _caller_user_id(request)
    deployed = _ensure_deployed(request, app_id)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserSkill

    factory = get_session_factory()
    async with factory() as db:
        rows = (
            await db.execute(
                select(UserSkill)
                .where(UserSkill.user_id == user_id)
                .where(UserSkill.app_id == app_id)
                .order_by(UserSkill.updated_at.desc())
            )
        ).scalars().all()
        user_items = [_serialize(r) for r in rows]

    return AppResponse(
        success=True,
        data={
            "app_skills": _app_skills(deployed),
            "user_skills": user_items,
            "allow_user_skills": _allow_user_skills(deployed),
        },
    )


@router.post("/{app_id}/skills", response_model=AppResponse)
async def create_skill(
    request: Request, app_id: str, body: SkillCreateRequest,
) -> AppResponse:
    """Create a user skill bound to the caller + this app. Rejected"""
    _validate_id(app_id)
    user_id = _caller_user_id(request)
    deployed = _ensure_deployed(request, app_id)
    _require_user_skills(deployed)

    name = _validate_name(body.name)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserSkill

    now = datetime.now(timezone.utc)
    row = UserSkill(
        id=str(uuid.uuid4()),
        user_id=user_id,
        app_id=app_id,
        name=name,
        description=body.description.strip(),
        instructions=body.instructions,
        created_at=now,
        updated_at=now,
    )
    factory = get_session_factory()
    async with factory() as db:
        existing = (
            await db.execute(
                select(UserSkill.id)
                .where(UserSkill.user_id == user_id)
                .where(UserSkill.app_id == app_id)
                .where(UserSkill.name == name)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"A skill named '{name}' already exists for this app.",
            )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return AppResponse(success=True, data={"skill": _serialize(row)})


@router.patch("/{app_id}/skills/{skill_id}", response_model=AppResponse)
async def update_skill(
    request: Request,
    app_id: str,
    skill_id: str,
    body: SkillUpdateRequest,
) -> AppResponse:
    """Partial update. 404 when the row doesn't belong to the caller"""
    _validate_id(app_id)
    user_id = _caller_user_id(request)
    deployed = _ensure_deployed(request, app_id)
    _require_user_skills(deployed)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserSkill

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(
                select(UserSkill)
                .where(UserSkill.id == skill_id)
                .where(UserSkill.user_id == user_id)
                .where(UserSkill.app_id == app_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Skill not found")

        sent = body.model_fields_set
        if "name" in sent and body.name is not None:
            new_name = _validate_name(body.name)
            if new_name != row.name:
                collision = (
                    await db.execute(
                        select(UserSkill.id)
                        .where(UserSkill.user_id == user_id)
                        .where(UserSkill.app_id == app_id)
                        .where(UserSkill.name == new_name)
                        .where(UserSkill.id != row.id)
                    )
                ).scalar_one_or_none()
                if collision is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"A skill named '{new_name}' already exists "
                            f"for this app."
                        ),
                    )
                row.name = new_name
        if "description" in sent and body.description is not None:
            row.description = body.description.strip()
        if "instructions" in sent and body.instructions is not None:
            row.instructions = body.instructions
        row.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
    return AppResponse(success=True, data={"skill": _serialize(row)})


@router.delete("/{app_id}/skills/{skill_id}", response_model=AppResponse)
async def delete_skill(
    request: Request, app_id: str, skill_id: str,
) -> AppResponse:
    """Hard delete. 404 when the row doesn't belong to the caller."""
    _validate_id(app_id)
    user_id = _caller_user_id(request)
    deployed = _ensure_deployed(request, app_id)
    _require_user_skills(deployed)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserSkill

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(
                select(UserSkill)
                .where(UserSkill.id == skill_id)
                .where(UserSkill.user_id == user_id)
                .where(UserSkill.app_id == app_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        await db.delete(row)
        await db.commit()
    return AppResponse(success=True, data={"deleted": True})
