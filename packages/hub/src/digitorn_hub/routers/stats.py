from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import bindparam, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import require_admin
from ..db import get_session
from ..models import (
    Package,
    PackageVersion,
    Publisher,
    Report,
    User,
)
from ..schemas import HubOverviewStats, PackageStatsOut, StatsBucket

router = APIRouter(tags=["stats"])


@router.get(
    "/packages/{publisher_slug}/{package_id}/stats",
    response_model=PackageStatsOut,
)
async def package_stats(
    publisher_slug: str,
    package_id: str,
    range_days: Annotated[int, Query(alias="range", ge=1, le=365)] = 30,
    session: AsyncSession = Depends(get_session),
) -> PackageStatsOut:
    pkg = (
        await session.execute(
            select(Package)
            .join(Publisher, Publisher.id == Package.publisher_id)
            .where(Publisher.slug == publisher_slug, Package.package_id == package_id)
        )
    ).scalar_one_or_none()
    if not pkg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "package not found")

    cutoff = datetime.now(timezone.utc) - timedelta(days=range_days)

    series_rows = (
        await session.execute(
            text(
                """
                SELECT to_char(date_trunc('day', e.occurred_at), 'YYYY-MM-DD') AS d,
                       count(*)::int AS n
                FROM hub.download_events e
                JOIN hub.package_versions v ON v.id = e.package_version_id
                WHERE v.package_id = :pkg AND e.occurred_at >= :cutoff
                GROUP BY 1
                ORDER BY 1
                """
            ).bindparams(bindparam("pkg", value=pkg.id), bindparam("cutoff", value=cutoff))
        )
    ).all()

    by_version_rows = (
        await session.execute(
            select(PackageVersion.version, PackageVersion.downloads)
            .where(PackageVersion.package_id == pkg.id)
            .order_by(PackageVersion.released_at.desc())
        )
    ).all()

    series = [StatsBucket(date=d, downloads=int(n)) for d, n in series_rows]
    total_in_range = sum(b.downloads for b in series)
    avg_per_day = round(total_in_range / max(range_days, 1), 2)

    return PackageStatsOut(
        publisher_slug=publisher_slug,
        package_id=package_id,
        range_days=range_days,
        total_downloads_in_range=total_in_range,
        avg_per_day=avg_per_day,
        series=series,
        by_version={v: int(n) for v, n in by_version_rows},
    )


@router.get(
    "/admin/stats/overview",
    response_model=HubOverviewStats,
    dependencies=[Depends(require_admin)],
)
async def admin_overview(
    session: AsyncSession = Depends(get_session),
) -> HubOverviewStats:
    async def _scalar(stmt):
        return int((await session.execute(stmt)).scalar_one() or 0)

    total_packages = await _scalar(select(func.count(Package.id)))
    total_versions = await _scalar(select(func.count(PackageVersion.id)))
    total_publishers = await _scalar(select(func.count(Publisher.id)))
    verified_publishers = await _scalar(
        select(func.count(Publisher.id)).where(Publisher.verified.is_(True))
    )
    total_users = await _scalar(select(func.count(User.id)))
    total_downloads = await _scalar(select(func.sum(Package.total_downloads)))

    now = datetime.now(timezone.utc)
    dl_24h = await _scalar(
        text("SELECT count(*) FROM hub.download_events WHERE occurred_at > :c").bindparams(
            bindparam("c", value=now - timedelta(hours=24))
        )
    )
    dl_7d = await _scalar(
        text("SELECT count(*) FROM hub.download_events WHERE occurred_at > :c").bindparams(
            bindparam("c", value=now - timedelta(days=7))
        )
    )
    open_reports = await _scalar(
        select(func.count(Report.id)).where(Report.status == "open")
    )

    return HubOverviewStats(
        total_packages=total_packages,
        total_versions=total_versions,
        total_publishers=total_publishers,
        verified_publishers=verified_publishers,
        total_users=total_users,
        total_downloads_all_time=total_downloads,
        downloads_last_24h=dl_24h,
        downloads_last_7d=dl_7d,
        open_reports=open_reports,
    )
