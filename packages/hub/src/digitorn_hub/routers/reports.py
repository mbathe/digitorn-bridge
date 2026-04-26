from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import AuthPrincipal, get_current_principal, require_admin
from ..db import get_session
from ..models import Package, Publisher, Report
from ..schemas import ReportCreate, ReportOut, ReportResolveIn

router = APIRouter(tags=["reports"])


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


def _to_out(report: Report, publisher_slug: str | None, package_id_str: str | None) -> ReportOut:
    return ReportOut(
        id=report.id,
        package_id=report.package_id,
        publisher_slug=publisher_slug,
        package_id_str=package_id_str,
        reporter_user_id=report.reporter_user_id,
        reason=report.reason,
        details=report.details,
        status=report.status,
        created_at=report.created_at,
        resolved_at=report.resolved_at,
        resolution_note=report.resolution_note,
    )


@router.post(
    "/packages/{publisher_slug}/{package_id}/reports",
    response_model=ReportOut,
    status_code=status.HTTP_201_CREATED,
)
async def submit_report(
    publisher_slug: str,
    package_id: str,
    payload: ReportCreate,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> ReportOut:
    """Submit a report against a package.

    Rate-limit: max 1 report per (user, package) per 24h.
    """
    pkg = await _resolve_package(session, publisher_slug, package_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    duplicate = (
        await session.execute(
            select(func.count(Report.id)).where(
                Report.package_id == pkg.id,
                Report.reporter_user_id == principal.user_id,
                Report.created_at > cutoff,
            )
        )
    ).scalar_one()
    if duplicate:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "you have already reported this package in the last 24 hours",
        )
    report = Report(
        package_id=pkg.id,
        reporter_user_id=principal.user_id,
        reason=payload.reason,
        details=payload.details,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return _to_out(report, publisher_slug, package_id)


@router.get(
    "/admin/reports",
    response_model=list[ReportOut],
    dependencies=[Depends(require_admin)],
)
async def admin_list_reports(
    status_filter: Annotated[
        str | None, Query(alias="status", pattern="^(open|reviewing|resolved|rejected)$")
    ] = None,
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    session: AsyncSession = Depends(get_session),
) -> list[ReportOut]:
    stmt = (
        select(Report, Publisher.slug, Package.package_id)
        .join(Package, Package.id == Report.package_id)
        .join(Publisher, Publisher.id == Package.publisher_id)
        .order_by(Report.created_at.desc())
    )
    if status_filter:
        stmt = stmt.where(Report.status == status_filter)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(stmt)).all()
    return [_to_out(r, slug, pid) for r, slug, pid in rows]


@router.post(
    "/admin/reports/{report_id}/resolve",
    response_model=ReportOut,
    dependencies=[Depends(require_admin)],
)
async def admin_resolve_report(
    report_id: uuid.UUID,
    payload: ReportResolveIn,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> ReportOut:
    report = await session.get(Report, report_id)
    if not report:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    report.status = payload.status
    report.resolution_note = payload.note
    if payload.status in ("resolved", "rejected"):
        report.resolved_at = datetime.now(timezone.utc)
        report.resolved_by_user_id = principal.user_id
    await session.commit()
    await session.refresh(report)

    pkg = await session.get(Package, report.package_id)
    pub_slug = None
    pkg_id_str = None
    if pkg:
        pkg_id_str = pkg.package_id
        pub = await session.get(Publisher, pkg.publisher_id)
        pub_slug = pub.slug if pub else None
    return _to_out(report, pub_slug, pkg_id_str)
