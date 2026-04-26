from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import versioning
from ..archive import ArchiveError, ParsedArchive, parse_and_validate
from ..auth.deps import AuthPrincipal, get_current_principal, require_scope
from ..db import get_session
from ..models import Package, PackageTag, PackageVersion, Publisher
from ..search import embed_document
from ..settings import get_settings
from ..storage import get_storage

router = APIRouter(
    prefix="/publishers/{publisher_slug}/packages",
    tags=["packages"],
)


# ---------- helpers ----------


async def _ensure_publisher_owned(
    publisher_slug: str, principal: AuthPrincipal, session: AsyncSession
) -> Publisher:
    pub = (
        await session.execute(select(Publisher).where(Publisher.slug == publisher_slug))
    ).scalar_one_or_none()
    if not pub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publisher not found")
    if pub.owner_user_id != principal.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not your publisher")
    return pub


async def _refresh_search_vector(session: AsyncSession, package_id: uuid.UUID) -> None:
    pkg = await session.get(Package, package_id)
    if not pkg:
        return
    tags = (
        await session.execute(select(PackageTag.tag).where(PackageTag.package_id == package_id))
    ).scalars().all()
    await session.execute(
        text(
            """
            UPDATE hub.packages SET
              search_vector = setweight(to_tsvector('simple', :name), 'A') ||
                              setweight(to_tsvector('simple', :tags), 'B') ||
                              setweight(to_tsvector('simple', :descr), 'C'),
              updated_at = now()
            WHERE id = :pkg_id
            """
        ).bindparams(
            bindparam("name", value=pkg.name or ""),
            bindparam("tags", value=" ".join(tags)),
            bindparam("descr", value=pkg.description or ""),
            bindparam("pkg_id", value=package_id),
        )
    )


async def _replace_tags(
    session: AsyncSession, package_id: uuid.UUID, tags: list[str]
) -> None:
    await session.execute(delete(PackageTag).where(PackageTag.package_id == package_id))
    if tags:
        await session.execute(
            pg_insert(PackageTag.__table__).values(
                [{"package_id": package_id, "tag": t} for t in tags]
            )
        )


def _archive_object_key(publisher_slug: str, parsed: ParsedArchive) -> str:
    return f"{publisher_slug}/{parsed.package_id}/{parsed.version}/{parsed.sha256[:12]}.tar.gz"


_CT_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
}


def _icon_object_key(publisher_slug: str, package_id: str, ext: str) -> str:
    return f"icons/{publisher_slug}/{package_id}.{ext}"


def _hub_icon_path(publisher_slug: str, package_id: str) -> str:
    """Relative path to the Hub's own /icon route. Stored as-is in
    ``Package.icon_url``; the catalog serialiser prepends
    ``hub_public_base_url`` at read time so flipping the env var
    rewrites every client URL with no DB backfill needed."""
    return f"/api/v1/packages/{publisher_slug}/{package_id}/icon"


async def _maybe_push_icon(
    publisher_slug: str,
    package_id: str,
    parsed: ParsedArchive,
    storage,
) -> tuple[str, str] | None:
    """Push the parsed icon bytes (if any) to the private S3 prefix
    and return ``(hub_url, ext)``.

    The caller writes ``hub_url`` into ``Package.icon_url`` (so clients
    fetch through our `/icon` streaming route) and ``ext`` into
    ``Package.icon_storage_ext`` (so the route knows which S3 key to
    read). Returns None when there's nothing to push, or when the S3
    upload fails — failure is non-fatal: the publish keeps going."""
    if parsed.icon_bytes is None or parsed.icon_content_type is None:
        return None
    ext = _CT_TO_EXT.get(parsed.icon_content_type)
    if ext is None:
        return None
    key = _icon_object_key(publisher_slug, package_id, ext)
    try:
        await storage.put_object(key, parsed.icon_bytes, parsed.icon_content_type)
    except Exception as exc:  # noqa: BLE001 — log + skip, never block publish
        import logging
        logging.getLogger(__name__).warning(
            "icon_upload_failed publisher=%s package=%s key=%s err=%s",
            publisher_slug, package_id, key, exc,
        )
        return None
    return _hub_icon_path(publisher_slug, package_id), ext


async def _maybe_update_latest(
    session: AsyncSession, package: Package, new_version: PackageVersion
) -> None:
    current_latest_version: str | None = None
    if package.latest_version_id:
        current = await session.get(PackageVersion, package.latest_version_id)
        if current and not current.yanked:
            current_latest_version = current.version

    if current_latest_version is None or versioning.is_greater(
        new_version.version, current_latest_version
    ):
        package.latest_version_id = new_version.id
        package.updated_at = datetime.now(timezone.utc)


# ---------- endpoints ----------


@router.post(
    "/{package_id}/versions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("packages:write"))],
)
async def upload_version(
    publisher_slug: str,
    package_id: str,
    file: Annotated[UploadFile, File(description="The .tar.gz archive of the package")],
    version: Annotated[str | None, Form(description="Optional override of package.toml version")] = None,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    settings = get_settings()
    pub = await _ensure_publisher_owned(publisher_slug, principal, session)

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")
    if len(raw) > settings.max_archive_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"archive exceeds {settings.max_archive_bytes} bytes",
        )

    try:
        parsed = parse_and_validate(raw)
    except ArchiveError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    if parsed.package_id != package_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"package.toml id {parsed.package_id!r} != URL {package_id!r}",
        )
    if version is not None and version != parsed.version:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"form version {version!r} != package.toml version {parsed.version!r}",
        )

    pkg_stmt = select(Package).where(
        Package.publisher_id == pub.id, Package.package_id == package_id
    )
    pkg = (await session.execute(pkg_stmt)).scalar_one_or_none()
    if pkg is None:
        pkg = Package(
            publisher_id=pub.id,
            package_id=parsed.package_id,
            name=parsed.name,
            description=parsed.description,
            category=parsed.category,
            risk_level=parsed.risk_level,
            icon_url=parsed.icon_url,
        )
        session.add(pkg)
        await session.flush()
    else:
        pkg.name = parsed.name
        pkg.description = parsed.description
        pkg.category = parsed.category
        pkg.risk_level = parsed.risk_level
        if parsed.icon_url:
            pkg.icon_url = parsed.icon_url

    object_key = _archive_object_key(publisher_slug, parsed)
    storage = get_storage()
    await storage.put_object(object_key, raw, content_type="application/gzip")

    # Extracted icon → push to the private prefix and override the
    # row's icon_url to point at our own `/icon` route. Done AFTER
    # the archive upload so a single round-trip to S3 isn't blocked
    # on icon-extraction failures, and BEFORE the PackageVersion
    # row is created so a publish that fails on version conflict
    # doesn't leak an icon for a non-existent row.
    icon_pushed = await _maybe_push_icon(
        publisher_slug, parsed.package_id, parsed, storage
    )
    if icon_pushed is not None:
        pkg.icon_url, pkg.icon_storage_ext = icon_pushed

    pv = PackageVersion(
        package_id=pkg.id,
        version=parsed.version,
        manifest_json=parsed.manifest,
        archive_object_key=object_key,
        archive_size=parsed.size,
        archive_sha256=parsed.sha256,
        schema_version=1,
    )
    session.add(pv)

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        await storage.delete_object(object_key)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"version {parsed.version!r} already exists for this package",
        )

    await _maybe_update_latest(session, pkg, pv)
    await _replace_tags(session, pkg.id, parsed.tags)
    await _refresh_search_vector(session, pkg.id)

    if pkg.latest_version_id == pv.id:
        try:
            vector = await embed_document(parsed.name, parsed.description, parsed.tags)
            if vector:
                await session.execute(
                    update(Package).where(Package.id == pkg.id).values(embedding=vector)
                )
        except Exception:  # noqa: BLE001 — embedding failures must not block publish
            pass

    await session.commit()
    await session.refresh(pkg)
    await session.refresh(pv)

    return {
        "package": {
            "id": str(pkg.id),
            "publisher_slug": publisher_slug,
            "package_id": pkg.package_id,
            "name": pkg.name,
            "latest_version": parsed.version,
            "tags": parsed.tags,
        },
        "version": {
            "id": str(pv.id),
            "version": pv.version,
            "archive_size": pv.archive_size,
            "archive_sha256": pv.archive_sha256,
            "released_at": pv.released_at.isoformat(),
        },
    }


@router.post(
    "/{package_id}/versions/{version}/yank",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_scope("packages:write"))],
)
async def yank_version(
    publisher_slug: str,
    package_id: str,
    version: str,
    reason: Annotated[str | None, Form()] = None,
    principal: AuthPrincipal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    pub = await _ensure_publisher_owned(publisher_slug, principal, session)
    pkg = (
        await session.execute(
            select(Package).where(
                Package.publisher_id == pub.id, Package.package_id == package_id
            )
        )
    ).scalar_one_or_none()
    if not pkg:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "package not found")
    pv = (
        await session.execute(
            select(PackageVersion).where(
                PackageVersion.package_id == pkg.id, PackageVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if not pv:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")

    if not pv.yanked:
        pv.yanked = True
        pv.yanked_reason = reason
        pv.yanked_at = datetime.now(timezone.utc)

    if pkg.latest_version_id == pv.id:
        new_latest = (
            await session.execute(
                select(PackageVersion)
                .where(
                    PackageVersion.package_id == pkg.id,
                    PackageVersion.id != pv.id,
                    PackageVersion.yanked.is_(False),
                )
            )
        ).scalars().all()
        if new_latest:
            best = max(new_latest, key=lambda v: versioning.parse(v.version))
            pkg.latest_version_id = best.id
        else:
            pkg.latest_version_id = None

    await session.commit()
    return {"yanked": True, "version": version, "reason": reason}
