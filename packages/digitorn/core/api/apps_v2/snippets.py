"""User snippet routes - CRUD on the per-user, per-app prompt"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ._shared import AppResponse, _validate_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["apps"])


class SnippetCreateRequest(BaseModel):
    """POST body for creating a snippet."""

    model_config = {"extra": "forbid"}

    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1)
    emoji: str | None = Field(default=None, max_length=16)
    tags: list[str] | None = Field(default=None)


class SnippetUpdateRequest(BaseModel):
    """PATCH body - every field optional. Only provided fields are"""

    model_config = {"extra": "forbid"}

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1)
    emoji: str | None = Field(default=None, max_length=16)
    tags: list[str] | None = Field(default=None)


def _caller_user_id(request: Request) -> str:
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(uid)


def _serialize(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "app_id": row.app_id,
        "title": row.title,
        "body": row.body,
        "emoji": row.emoji,
        "tags": list(row.tags) if row.tags else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/{app_id}/snippets", response_model=AppResponse)
async def list_snippets(request: Request, app_id: str) -> AppResponse:
    """List the calling user's snippets for this app, newest first."""
    _validate_id(app_id)
    user_id = _caller_user_id(request)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserSnippet

    factory = get_session_factory()
    async with factory() as db:
        rows = (
            await db.execute(
                select(UserSnippet)
                .where(UserSnippet.user_id == user_id)
                .where(UserSnippet.app_id == app_id)
                .order_by(UserSnippet.updated_at.desc())
            )
        ).scalars().all()
        items = [_serialize(r) for r in rows]
    return AppResponse(
        success=True,
        data={"snippets": items, "count": len(items)},
    )


@router.post("/{app_id}/snippets", response_model=AppResponse)
async def create_snippet(
    request: Request, app_id: str, body: SnippetCreateRequest,
) -> AppResponse:
    """Create a snippet bound to the calling user + this app."""
    _validate_id(app_id)
    user_id = _caller_user_id(request)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserSnippet

    now = datetime.now(timezone.utc)
    row = UserSnippet(
        id=str(uuid.uuid4()),
        user_id=user_id,
        app_id=app_id,
        title=body.title.strip(),
        body=body.body,
        emoji=(body.emoji or None),
        tags=body.tags,
        created_at=now,
        updated_at=now,
    )
    factory = get_session_factory()
    async with factory() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return AppResponse(success=True, data={"snippet": _serialize(row)})


@router.patch("/{app_id}/snippets/{snippet_id}", response_model=AppResponse)
async def update_snippet(
    request: Request,
    app_id: str,
    snippet_id: str,
    body: SnippetUpdateRequest,
) -> AppResponse:
    """Update a snippet in place. 404s when the row doesn't belong to"""
    _validate_id(app_id)
    user_id = _caller_user_id(request)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserSnippet

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(
                select(UserSnippet)
                .where(UserSnippet.id == snippet_id)
                .where(UserSnippet.user_id == user_id)
                .where(UserSnippet.app_id == app_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Snippet not found")

        if body.title is not None:
            row.title = body.title.strip()
        if body.body is not None:
            row.body = body.body
        sent = body.model_fields_set
        if "emoji" in sent:
            row.emoji = body.emoji or None
        if "tags" in sent:
            row.tags = body.tags
        row.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
    return AppResponse(success=True, data={"snippet": _serialize(row)})


@router.delete("/{app_id}/snippets/{snippet_id}", response_model=AppResponse)
async def delete_snippet(
    request: Request, app_id: str, snippet_id: str,
) -> AppResponse:
    """Hard delete. 404s when the row doesn't belong to the user."""
    _validate_id(app_id)
    user_id = _caller_user_id(request)

    from digitorn.core.database import get_session_factory
    from digitorn.core.models import UserSnippet

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(
                select(UserSnippet)
                .where(UserSnippet.id == snippet_id)
                .where(UserSnippet.user_id == user_id)
                .where(UserSnippet.app_id == app_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Snippet not found")
        await db.delete(row)
        await db.commit()
    return AppResponse(success=True, data={"deleted": True})
