from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import (
    AuthPrincipal,
    get_current_principal,
    require_admin,
    require_scope,
)
from ..db import get_session
from ..models import Publisher
from ..schemas import PublisherCreate, PublisherOut, PublisherUpdate

router = APIRouter(prefix="/publishers", tags=["publishers"])


@router.post(
    "",
    response_model=PublisherOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("publishers:write"))],
)
async def create_publisher(
    payload: PublisherCreate,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Publisher:
    pub = Publisher(
        owner_user_id=principal.user_id,
        slug=payload.slug,
        name=payload.name,
        bio=payload.bio,
        website=payload.website,
    )
    session.add(pub)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "publisher slug already taken")
    await session.refresh(pub)
    return pub


@router.get("/me", response_model=list[PublisherOut])
async def list_my_publishers(
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> list[Publisher]:
    stmt = (
        select(Publisher)
        .where(Publisher.owner_user_id == principal.user_id)
        .order_by(Publisher.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars())


@router.get("/{slug}", response_model=PublisherOut)
async def get_publisher(
    slug: str, session: AsyncSession = Depends(get_session)
) -> Publisher:
    stmt = select(Publisher).where(Publisher.slug == slug)
    pub = (await session.execute(stmt)).scalar_one_or_none()
    if not pub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publisher not found")
    return pub


@router.post(
    "/{slug}/verify",
    response_model=PublisherOut,
    dependencies=[Depends(require_admin)],
)
async def admin_verify_publisher(
    slug: str,
    verified: bool = True,
    session: AsyncSession = Depends(get_session),
) -> Publisher:
    """Admin-only: grant or revoke the `verified` badge for a publisher."""
    pub = (
        await session.execute(select(Publisher).where(Publisher.slug == slug))
    ).scalar_one_or_none()
    if not pub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publisher not found")
    pub.verified = bool(verified)
    await session.commit()
    await session.refresh(pub)
    return pub


@router.patch("/{slug}", response_model=PublisherOut)
async def update_publisher(
    slug: str,
    payload: PublisherUpdate,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Publisher:
    stmt = select(Publisher).where(Publisher.slug == slug)
    pub = (await session.execute(stmt)).scalar_one_or_none()
    if not pub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publisher not found")
    if pub.owner_user_id != principal.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your publisher")
    if payload.name is not None:
        pub.name = payload.name
    if payload.bio is not None:
        pub.bio = payload.bio
    if payload.website is not None:
        pub.website = payload.website
    await session.commit()
    await session.refresh(pub)
    return pub
