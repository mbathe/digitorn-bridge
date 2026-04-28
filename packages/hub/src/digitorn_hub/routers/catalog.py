from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import DownloadEvent, Package, PackageTag, PackageVersion, Publisher
from ..schemas import (
    PackageDetailOut,
    PackageSummaryOut,
    PackageVersionOut,
    SearchHit,
    SearchResponse,
)
from ..search import embed_query, reciprocal_rank_fusion
from ..settings import get_settings
from ..storage import get_storage

router = APIRouter(tags=["catalog"])


# ---------- helpers ----------


def _resolve_icon_url(icon_url: str | None) -> str | None:
    """Decide what URL to expose to clients for a package icon.

    Stored values:
      - ``None``                              → no icon, return None
      - ``http(s)://...`` (publisher-hosted)  → return as-is
      - ``/api/v1/packages/.../icon`` (Hub)   → prepend ``hub_public_base_url``
        when it's set (so cross-origin clients get an absolute URL),
        otherwise return as-is (relative - works for same-origin only).

    Recomposing at serialisation time means flipping the env var
    ``HUB_HUB_PUBLIC_BASE_URL`` immediately fixes every client without
    a DB backfill."""
    if not icon_url:
        return None
    if icon_url.startswith(("http://", "https://")):
        return icon_url
    if icon_url.startswith("/"):
        base = (get_settings().hub_public_base_url or "").rstrip("/")
        return f"{base}{icon_url}" if base else icon_url
    # Anything else (legacy bogus values like "icon.png" or an emoji)
    # - drop silently so the client falls back to the seeded initial
    # avatar instead of a broken <img>.
    return None


async def _load_summaries(
    session: AsyncSession, package_ids: list[uuid.UUID]
) -> dict[uuid.UUID, PackageSummaryOut]:
    if not package_ids:
        return {}
    rows = (
        await session.execute(
            select(
                Package,
                Publisher.slug,
                Publisher.verified,
                PackageVersion.version,
            )
            .join(Publisher, Publisher.id == Package.publisher_id)
            .outerjoin(
                PackageVersion, PackageVersion.id == Package.latest_version_id
            )
            .where(Package.id.in_(package_ids))
        )
    ).all()

    tag_rows = (
        await session.execute(
            select(PackageTag.package_id, PackageTag.tag).where(
                PackageTag.package_id.in_(package_ids)
            )
        )
    ).all()
    tags_by_pkg: dict[uuid.UUID, list[str]] = {}
    for pkg_id, tag in tag_rows:
        tags_by_pkg.setdefault(pkg_id, []).append(tag)

    out: dict[uuid.UUID, PackageSummaryOut] = {}
    for pkg, publisher_slug, publisher_verified, latest_version in rows:
        out[pkg.id] = PackageSummaryOut(
            id=pkg.id,
            publisher_slug=publisher_slug,
            publisher_verified=publisher_verified,
            package_id=pkg.package_id,
            name=pkg.name,
            description=pkg.description,
            category=pkg.category,
            risk_level=pkg.risk_level,
            icon_url=_resolve_icon_url(pkg.icon_url),
            latest_version=latest_version,
            total_downloads=pkg.total_downloads,
            avg_rating=float(pkg.avg_rating) if pkg.avg_rating is not None else None,
            review_count=int(pkg.review_count or 0),
            tags=sorted(tags_by_pkg.get(pkg.id, [])),
            updated_at=pkg.updated_at,
        )
    return out


def _normalize_query(q: str) -> str:
    return " ".join(q.split())


# ---------- search ----------


@router.get("/search", response_model=SearchResponse)
async def search(
    q: Annotated[str, Query(min_length=0, max_length=200)] = "",
    category: Annotated[str | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    risk_level: Annotated[str | None, Query(pattern="^(low|medium|high)$")] = None,
    publisher: Annotated[str | None, Query()] = None,
    include_unverified: Annotated[
        bool,
        Query(description="Include packages from non-verified publishers (default: false)"),
    ] = False,
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    session: AsyncSession = Depends(get_session),
) -> SearchResponse:
    """Hybrid search:
       - Postgres FTS on `search_vector` (name=A, tags=B, description=C)
       - Semantic kNN on `embedding` (cosine)
       - RRF combination, k=60
       Filters (`category`, `tag`, `risk_level`, `publisher`) are applied AFTER fusion.

       By default, results are restricted to packages from VERIFIED publishers.
       Set `include_unverified=true` to see community packages too.
    """
    q_norm = _normalize_query(q)
    candidate_limit = 100

    fts_ids: list[uuid.UUID] = []
    semantic_ids: list[uuid.UUID] = []

    if q_norm:
        fts_stmt = text(
            """
            SELECT p.id
            FROM hub.packages p
            WHERE p.search_vector @@ websearch_to_tsquery('simple', :q)
            ORDER BY ts_rank_cd(p.search_vector,
                                 websearch_to_tsquery('simple', :q)) DESC,
                     p.total_downloads DESC
            LIMIT :lim
            """
        ).bindparams(
            bindparam("q", value=q_norm),
            bindparam("lim", value=candidate_limit),
        )
        fts_ids = list((await session.execute(fts_stmt)).scalars())

        try:
            qvec = await embed_query(q_norm)
        except Exception:  # noqa: BLE001
            qvec = []
        if qvec:
            settings = get_settings()
            sem_stmt = text(
                """
                SELECT p.id
                FROM hub.packages p
                WHERE p.embedding IS NOT NULL
                  AND (p.embedding <=> CAST(:vec AS vector)) < :max_dist
                ORDER BY p.embedding <=> CAST(:vec AS vector)
                LIMIT :lim
                """
            ).bindparams(
                bindparam("vec", value=str(qvec)),
                bindparam("max_dist", value=settings.semantic_max_distance),
                bindparam("lim", value=candidate_limit),
            )
            semantic_ids = list((await session.execute(sem_stmt)).scalars())

    if not q_norm:
        # No query: popularity browse, with filters applied via SQL
        pop_stmt = (
            select(Package.id)
            .order_by(Package.total_downloads.desc(), Package.updated_at.desc())
            .limit(candidate_limit)
        )
        candidate_ids = list((await session.execute(pop_stmt)).scalars())
        ranked = [(pid, 1.0 / (60 + i)) for i, pid in enumerate(candidate_ids, 1)]
    else:
        ranked = reciprocal_rank_fusion([fts_ids, semantic_ids])

    if not ranked:
        return SearchResponse(query=q_norm, total=0, page=page, page_size=page_size, hits=[])

    candidate_ids = [pid for pid, _ in ranked]

    filt_stmt = select(Package.id).join(
        Publisher, Publisher.id == Package.publisher_id
    ).where(Package.id.in_(candidate_ids))
    if not include_unverified:
        filt_stmt = filt_stmt.where(Publisher.verified.is_(True))
    if category:
        filt_stmt = filt_stmt.where(Package.category == category)
    if risk_level:
        filt_stmt = filt_stmt.where(Package.risk_level == risk_level)
    if publisher:
        filt_stmt = filt_stmt.where(Publisher.slug == publisher)
    if tag:
        filt_stmt = filt_stmt.where(
            Package.id.in_(select(PackageTag.package_id).where(PackageTag.tag.in_(tag)))
        )
    keep = set((await session.execute(filt_stmt)).scalars())

    filtered = [(pid, score) for pid, score in ranked if pid in keep]
    total = len(filtered)
    start = (page - 1) * page_size
    page_slice = filtered[start : start + page_size]

    summaries = await _load_summaries(session, [pid for pid, _ in page_slice])
    hits = [
        SearchHit(**summaries[pid].model_dump(), score=round(score, 6))
        for pid, score in page_slice
        if pid in summaries
    ]
    return SearchResponse(
        query=q_norm, total=total, page=page, page_size=page_size, hits=hits
    )


# ---------- public package read endpoints ----------


@router.get(
    "/packages/{publisher_slug}/{package_id}",
    response_model=PackageDetailOut,
)
async def get_package(
    publisher_slug: str,
    package_id: str,
    session: AsyncSession = Depends(get_session),
) -> PackageDetailOut:
    pub = (
        await session.execute(select(Publisher).where(Publisher.slug == publisher_slug))
    ).scalar_one_or_none()
    if not pub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publisher not found")

    pkg = (
        await session.execute(
            select(Package).where(
                Package.publisher_id == pub.id, Package.package_id == package_id
            )
        )
    ).scalar_one_or_none()
    if not pkg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "package not found")

    summaries = await _load_summaries(session, [pkg.id])
    summary = summaries[pkg.id]

    versions = list(
        (
            await session.execute(
                select(PackageVersion)
                .where(PackageVersion.package_id == pkg.id)
                .order_by(PackageVersion.released_at.desc())
            )
        ).scalars()
    )

    latest_manifest: dict = {}
    if pkg.latest_version_id:
        latest = next((v for v in versions if v.id == pkg.latest_version_id), None)
        if latest:
            latest_manifest = latest.manifest_json or {}

    return PackageDetailOut(
        **summary.model_dump(),
        versions=[PackageVersionOut.model_validate(v) for v in versions],
        manifest=latest_manifest,
    )


_ICON_EXT_TO_CT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "ico": "image/x-icon",
}


@router.get(
    "/packages/{publisher_slug}/{package_id}/icon",
    responses={
        200: {"content": {"image/*": {}}, "description": "Icon bytes."},
        404: {"description": "No icon for this package."},
    },
)
async def get_package_icon(
    publisher_slug: str,
    package_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stream the icon bytes for a package.

    Public endpoint (no auth) so browser ``<img>`` tags can fetch
    directly. Cache-Control: 1 day so the same browser doesn't hit
    us repeatedly. The bytes live on the (private) S3 bucket under
    ``icons/{publisher_slug}/{package_id}.{ext}`` - we read once per
    cache miss.
    """
    stmt = (
        select(Package.icon_storage_ext)
        .join(Publisher, Publisher.id == Package.publisher_id)
        .where(
            Publisher.slug == publisher_slug,
            Package.package_id == package_id,
        )
    )
    ext = (await session.execute(stmt)).scalar_one_or_none()
    if not ext:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no icon")
    content_type = _ICON_EXT_TO_CT.get(ext.lower())
    if content_type is None:
        # icon_storage_ext is set but to an unknown ext - defensive,
        # shouldn't happen because we validate ext on upload.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown icon type")

    key = f"icons/{publisher_slug}/{package_id}.{ext}"
    storage = get_storage()
    try:
        data = await storage.get_object_bytes(key)
    except Exception as exc:  # noqa: BLE001
        # S3 missing or transient error - degrade to 404 so the client
        # falls back to its initial-avatar placeholder.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "icon unavailable") from exc

    return Response(
        content=data,
        media_type=content_type,
        headers={
            # 1 day public cache. Republishing the same icon overwrites
            # the S3 key, so cached copies become stale for at most 1
            # day - acceptable for icons.
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.get(
    "/packages/{publisher_slug}/{package_id}/versions/{version}",
    response_model=PackageVersionOut,
)
async def get_version(
    publisher_slug: str,
    package_id: str,
    version: str,
    session: AsyncSession = Depends(get_session),
) -> PackageVersion:
    stmt = (
        select(PackageVersion)
        .join(Package, Package.id == PackageVersion.package_id)
        .join(Publisher, Publisher.id == Package.publisher_id)
        .where(
            Publisher.slug == publisher_slug,
            Package.package_id == package_id,
            PackageVersion.version == version,
        )
    )
    pv = (await session.execute(stmt)).scalar_one_or_none()
    if not pv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")
    return pv


@router.get(
    "/packages/{publisher_slug}/{package_id}/versions/{version}/download",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
)
async def download_version(
    publisher_slug: str,
    package_id: str,
    version: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    settings = get_settings()
    stmt = (
        select(PackageVersion, Package.id)
        .join(Package, Package.id == PackageVersion.package_id)
        .join(Publisher, Publisher.id == Package.publisher_id)
        .where(
            Publisher.slug == publisher_slug,
            Package.package_id == package_id,
            PackageVersion.version == version,
        )
    )
    row = (await session.execute(stmt)).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")
    pv, pkg_id = row
    if pv.yanked:
        raise HTTPException(
            status.HTTP_410_GONE, f"version {version} has been yanked"
        )

    storage = get_storage()
    url = await storage.presigned_get_url(
        pv.archive_object_key, settings.s3_presign_ttl_seconds
    )

    client_ip = (request.client.host if request.client else "") or ""
    ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest() if client_ip else None
    daemon_fp = request.headers.get("x-digitorn-fingerprint")
    user_agent = request.headers.get("user-agent")

    session.add(
        DownloadEvent(
            package_version_id=pv.id,
            daemon_fingerprint=daemon_fp[:64] if daemon_fp else None,
            ip_hash=ip_hash,
            user_agent=user_agent[:255] if user_agent else None,
        )
    )
    await session.execute(
        update(PackageVersion)
        .where(PackageVersion.id == pv.id)
        .values(downloads=PackageVersion.downloads + 1)
    )
    await session.execute(
        update(Package)
        .where(Package.id == pkg_id)
        .values(total_downloads=Package.total_downloads + 1)
    )
    await session.commit()

    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/packages", response_model=SearchResponse)
async def list_packages(
    category: Annotated[str | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    include_unverified: Annotated[bool, Query()] = False,
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    session: AsyncSession = Depends(get_session),
) -> SearchResponse:
    return await search(
        q="",
        category=category,
        tag=tag,
        risk_level=None,
        publisher=None,
        include_unverified=include_unverified,
        page=page,
        page_size=page_size,
        session=session,
    )
