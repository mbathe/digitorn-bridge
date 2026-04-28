from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import (
    AuthPrincipal,
    get_current_principal,
    require_admin,
)
from ..db import get_session
from ..models import Package, Publisher, Review, User
from ..schemas import (
    ReviewCreate,
    ReviewListResponse,
    ReviewOut,
)

router = APIRouter(tags=["reviews"])


# ── helpers ─────────────────────────────────────────────────────────


async def _resolve_package(
    session: AsyncSession, publisher_slug: str, package_id: str
) -> Package:
    pkg = (
        await session.execute(
            select(Package)
            .join(Publisher, Publisher.id == Package.publisher_id)
            .where(Publisher.slug == publisher_slug, Package.package_id == package_id)
        )
    ).scalar_one_or_none()
    if not pkg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "package not found")
    return pkg


async def _check_not_self_review(
    session: AsyncSession, user_id: uuid.UUID, pkg: Package
) -> None:
    pub = await session.get(Publisher, pkg.publisher_id)
    if pub and pub.owner_user_id == user_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "cannot review a package you publish yourself",
        )


async def _check_rate_limit(
    session: AsyncSession, user_id: uuid.UUID, package_id: uuid.UUID
) -> None:
    """Allow at most one NEW review per (user, package) per hour.

    A user already having a review for this package can update it freely
    (this only stops *creation* spam, not legit edits).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    recent = (
        await session.execute(
            select(func.count(Review.id))
            .where(
                Review.user_id == user_id,
                Review.created_at > cutoff,
            )
        )
    ).scalar_one()
    if recent >= 5:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "review rate limit reached (max 5 reviews per hour)",
        )


async def _recompute_aggregates(
    session: AsyncSession, package_id: uuid.UUID
) -> None:
    await session.execute(
        text(
            """
            UPDATE hub.packages SET
              avg_rating = sub.avg_rating,
              review_count = sub.review_count
            FROM (
              SELECT
                COALESCE(AVG(rating)::numeric(3,2), NULL) AS avg_rating,
                COUNT(*) AS review_count
              FROM hub.reviews
              WHERE package_id = :pkg AND hidden = false
            ) sub
            WHERE id = :pkg
            """
        ).bindparams(bindparam("pkg", value=package_id))
    )


def _to_out(review: Review, display_name: str | None) -> ReviewOut:
    return ReviewOut(
        id=review.id,
        package_id=review.package_id,
        user_id=review.user_id,
        user_display_name=display_name,
        rating=review.rating,
        body=review.body,
        hidden=review.hidden,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


# ── endpoints ────────────────────────────────────────────────────────


@router.post(
    "/packages/{publisher_slug}/{package_id}/reviews",
    response_model=ReviewOut,
    status_code=status.HTTP_201_CREATED,
)
async def submit_review(
    publisher_slug: str,
    package_id: str,
    payload: ReviewCreate,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> ReviewOut:
    """Create or update the caller's review for a package.

    Rules:
      - one review per (user, package) - re-submitting overwrites the previous one
      - cannot review a package owned by your own publisher
      - rate-limit: 5 new-review creations per hour per user
    """
    pkg = await _resolve_package(session, publisher_slug, package_id)
    await _check_not_self_review(session, principal.user_id, pkg)

    existing = (
        await session.execute(
            select(Review).where(
                Review.package_id == pkg.id, Review.user_id == principal.user_id
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        await _check_rate_limit(session, principal.user_id, pkg.id)
        review = Review(
            package_id=pkg.id,
            user_id=principal.user_id,
            rating=payload.rating,
            body=payload.body,
        )
        session.add(review)
    else:
        existing.rating = payload.rating
        existing.body = payload.body
        review = existing

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "review constraint violation")

    await _recompute_aggregates(session, pkg.id)
    await session.commit()
    await session.refresh(review)
    return _to_out(review, principal.user.display_name)


@router.get(
    "/packages/{publisher_slug}/{package_id}/reviews",
    response_model=ReviewListResponse,
)
async def list_reviews(
    publisher_slug: str,
    package_id: str,
    sort: Annotated[str, Query(pattern="^(recent|rating_desc|rating_asc)$")] = "recent",
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    session: AsyncSession = Depends(get_session),
) -> ReviewListResponse:
    pkg = await _resolve_package(session, publisher_slug, package_id)

    base = select(Review, User.display_name).join(
        User, User.id == Review.user_id
    ).where(Review.package_id == pkg.id, Review.hidden.is_(False))
    if sort == "recent":
        base = base.order_by(Review.created_at.desc())
    elif sort == "rating_desc":
        base = base.order_by(Review.rating.desc(), Review.created_at.desc())
    else:
        base = base.order_by(Review.rating.asc(), Review.created_at.desc())

    total = (
        await session.execute(
            select(func.count(Review.id)).where(
                Review.package_id == pkg.id, Review.hidden.is_(False)
            )
        )
    ).scalar_one()

    rows = (
        await session.execute(
            base.offset((page - 1) * page_size).limit(page_size)
        )
    ).all()

    distribution_rows = (
        await session.execute(
            select(Review.rating, func.count(Review.id))
            .where(Review.package_id == pkg.id, Review.hidden.is_(False))
            .group_by(Review.rating)
        )
    ).all()
    distribution = {n: 0 for n in range(1, 6)}
    for rating, n in distribution_rows:
        distribution[int(rating)] = int(n)

    items = [_to_out(r, name) for r, name in rows]
    return ReviewListResponse(
        total=int(total),
        page=page,
        page_size=page_size,
        avg_rating=float(pkg.avg_rating) if pkg.avg_rating is not None else None,
        review_count=int(pkg.review_count),
        distribution=distribution,
        items=items,
    )


@router.delete(
    "/reviews/{review_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_review(
    review_id: uuid.UUID,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> None:
    review = await session.get(Review, review_id)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "review not found")
    if review.user_id != principal.user_id and principal.user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your review")
    pkg_id = review.package_id
    await session.delete(review)
    await _recompute_aggregates(session, pkg_id)
    await session.commit()


@router.post(
    "/admin/reviews/{review_id}/hide",
    response_model=ReviewOut,
    dependencies=[Depends(require_admin)],
)
async def admin_hide_review(
    review_id: uuid.UUID,
    reason: Annotated[str | None, Query(max_length=2000)] = None,
    session: AsyncSession = Depends(get_session),
) -> ReviewOut:
    review = await session.get(Review, review_id)
    if not review:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "review not found")
    review.hidden = True
    review.hidden_reason = reason
    await _recompute_aggregates(session, review.package_id)
    await session.commit()
    await session.refresh(review)
    user = await session.get(User, review.user_id)
    return _to_out(review, user.display_name if user else None)
