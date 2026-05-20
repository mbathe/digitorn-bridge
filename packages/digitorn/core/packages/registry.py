"""PackageRegistry - CRUD over the `installed_packages` table."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

class Status:
    """Lifecycle states for InstalledPackage rows."""

    INSTALLING = "installing"
    INSTALLED = "installed"
    BROKEN = "broken"
    UPGRADING = "upgrading"
    DEGRADED = "degraded"
    UNINSTALLING = "uninstalling"

    ALL = (
        INSTALLING, INSTALLED, BROKEN, UPGRADING, DEGRADED, UNINSTALLING,
    )

class SourceType:
    """How a package was installed."""

    BUILTIN = "builtin"
    LOCAL = "local"
    HUB = "hub"
    GIT = "git"

    ALL = (BUILTIN, LOCAL, HUB, GIT)

class PackageNotFound(Exception):
    """Raised when a registry lookup misses."""

class Scope:
    """Package install scope."""

    SYSTEM = "system"
    USER = "user"
    ALL = (SYSTEM, USER)

class PackageRegistry:
    """Async CRUD store for installed_packages."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        package_id: str,
        source_type: str,
        source_uri: str,
        version: str,
        hash: str,
        manifest: dict[str, Any],
        installed_by: str = "",
        status: str = Status.INSTALLED,
        scope: str = Scope.SYSTEM,
        owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Register a fresh installation."""
        from digitorn.core.models import InstalledPackage

        if source_type not in SourceType.ALL:
            raise ValueError(
                f"unknown source_type {source_type!r} (allowed: {SourceType.ALL})"
            )
        if status not in Status.ALL:
            raise ValueError(
                f"unknown status {status!r} (allowed: {Status.ALL})"
            )
        if scope not in Scope.ALL:
            raise ValueError(
                f"unknown scope {scope!r} (allowed: {Scope.ALL})"
            )
        if scope == Scope.USER and not owner_user_id:
            raise ValueError("scope='user' requires an owner_user_id")
        if scope == Scope.SYSTEM and owner_user_id:
            raise ValueError("scope='system' must not have an owner_user_id")

        async with self._session_factory() as db:
            # Replace existing row for the SAME (package_id, scope,
            # owner_user_id) tuple - not across scopes.
            stmt = select(InstalledPackage).where(
                InstalledPackage.package_id == package_id,
                InstalledPackage.scope == scope,
            )
            if owner_user_id is None:
                stmt = stmt.where(InstalledPackage.owner_user_id.is_(None))
            else:
                stmt = stmt.where(
                    InstalledPackage.owner_user_id == owner_user_id
                )
            existing = (await db.execute(stmt)).scalar_one_or_none()

            if existing is None:
                row = InstalledPackage(
                    package_id=package_id,
                    scope=scope,
                    owner_user_id=owner_user_id,
                    source_type=source_type,
                    source_uri=source_uri,
                    version=version,
                    hash=hash,
                    manifest=manifest,
                    status=status,
                    installed_by=installed_by,
                )
                db.add(row)
            else:
                row = existing
                row.source_type = source_type
                row.source_uri = source_uri
                row.version = version
                row.hash = hash
                row.manifest = manifest
                row.status = status
                row.installed_by = installed_by
                row.last_error = None
                row.updated_at = datetime.now(timezone.utc)
                flag_modified(row, "manifest")

            await db.commit()
            await db.refresh(row)
            return self._row_to_dict(row)

    async def get(
        self,
        package_id: str,
        *,
        scope: str | None = None,
        owner_user_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get one installed package row."""
        from digitorn.core.models import InstalledPackage

        async with self._session_factory() as db:
            stmt = select(InstalledPackage).where(
                InstalledPackage.package_id == package_id,
            )
            if scope is not None:
                stmt = stmt.where(InstalledPackage.scope == scope)
            if owner_user_id is not None:
                stmt = stmt.where(
                    InstalledPackage.owner_user_id == owner_user_id
                )
            elif scope == Scope.SYSTEM:
                stmt = stmt.where(InstalledPackage.owner_user_id.is_(None))

            # Deterministic ordering: system first, then by recency.
            stmt = stmt.order_by(
                InstalledPackage.scope.asc(),
                InstalledPackage.updated_at.desc(),
            )
            result = await db.execute(stmt)
            row = result.scalars().first()
            return self._row_to_dict(row) if row else None

    async def resolve_for_caller(
        self,
        package_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        """Return the install the caller sees for a given package_id."""
        user_row = await self.get(
            package_id, scope=Scope.USER, owner_user_id=user_id,
        )
        if user_row is not None:
            return user_row
        return await self.get(package_id, scope=Scope.SYSTEM)

    async def list_all(
        self,
        *,
        source_type: str | None = None,
        status: str | None = None,
        scope: str | None = None,
        owner_user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List every installed package, optionally filtered."""
        from digitorn.core.models import InstalledPackage

        async with self._session_factory() as db:
            stmt = select(InstalledPackage).order_by(
                InstalledPackage.installed_at.desc()
            )
            if source_type is not None:
                stmt = stmt.where(InstalledPackage.source_type == source_type)
            if status is not None:
                stmt = stmt.where(InstalledPackage.status == status)
            if scope is not None:
                stmt = stmt.where(InstalledPackage.scope == scope)
            if owner_user_id is not None:
                stmt = stmt.where(
                    InstalledPackage.owner_user_id == owner_user_id
                )
            rows = (await db.execute(stmt)).scalars().all()
            return [self._row_to_dict(r) for r in rows]

    async def list_visible_to_user(
        self,
        *,
        user_id: str,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List packages visible to one user: their own + system."""
        from digitorn.core.models import InstalledPackage
        from sqlalchemy import or_

        async with self._session_factory() as db:
            stmt = select(InstalledPackage).where(
                or_(
                    InstalledPackage.scope == Scope.SYSTEM,
                    (
                        (InstalledPackage.scope == Scope.USER)
                        & (InstalledPackage.owner_user_id == user_id)
                    ),
                )
            )
            if source_type is not None:
                stmt = stmt.where(InstalledPackage.source_type == source_type)
            if status is not None:
                stmt = stmt.where(InstalledPackage.status == status)
            stmt = stmt.order_by(InstalledPackage.installed_at.desc())
            rows = (await db.execute(stmt)).scalars().all()

            # Collapse: when a package_id has both system + user
            # rows, keep the user row (it shadows the system one).
            by_id: dict[str, Any] = {}
            for r in rows:
                pid = r.package_id
                if pid in by_id:
                    # Prefer the user row
                    if by_id[pid].scope == Scope.SYSTEM and r.scope == Scope.USER:
                        by_id[pid] = r
                else:
                    by_id[pid] = r
            return [self._row_to_dict(r) for r in by_id.values()]

    async def exists(
        self,
        package_id: str,
        *,
        scope: str | None = None,
        owner_user_id: str | None = None,
    ) -> bool:
        return (
            await self.get(
                package_id, scope=scope, owner_user_id=owner_user_id,
            )
        ) is not None

    def _scope_filter(
        self,
        stmt: Any,
        scope: str | None,
        owner_user_id: str | None,
    ) -> Any:
        from digitorn.core.models import InstalledPackage
        if scope is not None:
            stmt = stmt.where(InstalledPackage.scope == scope)
        if owner_user_id is not None:
            stmt = stmt.where(
                InstalledPackage.owner_user_id == owner_user_id
            )
        elif scope == Scope.SYSTEM:
            stmt = stmt.where(InstalledPackage.owner_user_id.is_(None))
        return stmt

    async def update_status(
        self,
        package_id: str,
        *,
        status: str,
        last_error: str | None = None,
        scope: str | None = None,
        owner_user_id: str | None = None,
    ) -> bool:
        from digitorn.core.models import InstalledPackage

        if status not in Status.ALL:
            raise ValueError(f"unknown status {status!r}")

        async with self._session_factory() as db:
            stmt = select(InstalledPackage).where(
                InstalledPackage.package_id == package_id,
            )
            stmt = self._scope_filter(stmt, scope, owner_user_id)
            # Deterministic pick when no scope filter - prefer system
            stmt = stmt.order_by(InstalledPackage.scope.asc())
            row = (await db.execute(stmt)).scalars().first()
            if row is None:
                return False
            row.status = status
            row.last_error = last_error
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()
            return True

    async def update_hash(
        self,
        package_id: str,
        *,
        new_hash: str,
        scope: str | None = None,
        owner_user_id: str | None = None,
    ) -> bool:
        """Bump the recorded hash after an upgrade."""
        from digitorn.core.models import InstalledPackage

        async with self._session_factory() as db:
            stmt = select(InstalledPackage).where(
                InstalledPackage.package_id == package_id,
            )
            stmt = self._scope_filter(stmt, scope, owner_user_id)
            # Deterministic pick when no scope filter - prefer system
            stmt = stmt.order_by(InstalledPackage.scope.asc())
            row = (await db.execute(stmt)).scalars().first()
            if row is None:
                return False
            row.hash = new_hash
            row.updated_at = datetime.now(timezone.utc)
            await db.commit()
            return True

    async def update_version(
        self,
        package_id: str,
        *,
        new_version: str,
        new_hash: str,
        new_manifest: dict[str, Any],
        scope: str | None = None,
        owner_user_id: str | None = None,
    ) -> bool:
        """Single atomic update for an upgrade - version + hash + manifest."""
        from digitorn.core.models import InstalledPackage

        async with self._session_factory() as db:
            stmt = select(InstalledPackage).where(
                InstalledPackage.package_id == package_id,
            )
            stmt = self._scope_filter(stmt, scope, owner_user_id)
            # Deterministic pick when no scope filter - prefer system
            stmt = stmt.order_by(InstalledPackage.scope.asc())
            row = (await db.execute(stmt)).scalars().first()
            if row is None:
                return False
            row.version = new_version
            row.hash = new_hash
            row.manifest = new_manifest
            row.status = Status.INSTALLED
            row.last_error = None
            row.updated_at = datetime.now(timezone.utc)
            flag_modified(row, "manifest")
            await db.commit()
            return True

    async def delete(
        self,
        package_id: str,
        *,
        scope: str | None = None,
        owner_user_id: str | None = None,
    ) -> bool:
        """Hard-delete the row. Wiping files on disk is."""
        from digitorn.core.models import InstalledPackage

        async with self._session_factory() as db:
            stmt = delete(InstalledPackage).where(
                InstalledPackage.package_id == package_id,
            )
            if scope is not None:
                stmt = stmt.where(InstalledPackage.scope == scope)
            if owner_user_id is not None:
                stmt = stmt.where(
                    InstalledPackage.owner_user_id == owner_user_id
                )
            elif scope == Scope.SYSTEM:
                stmt = stmt.where(InstalledPackage.owner_user_id.is_(None))
            result = await db.execute(stmt)
            await db.commit()
            return (result.rowcount or 0) > 0

    async def check_drift(self, package_id: str) -> dict[str, Any]:
        """Compare on-disk content hash with the registered hash."""
        import asyncio as _asyncio
        from digitorn.core.packages.hash import compute_package_hash
        from digitorn.core.packages.resolver import _app_dir

        row = await self.get(package_id)
        if row is None:
            raise PackageNotFound(package_id)

        owner = row.get("owner_user_id") or None
        install_dir = _app_dir(package_id, user_id=owner)
        if not install_dir.is_dir():
            return {
                "drifted": True,
                "current_hash": "",
                "stored_hash": row["hash"],
                "install_dir": str(install_dir),
                "missing": True,
            }

        try:
            current = await _asyncio.to_thread(compute_package_hash, install_dir)
        except Exception as exc:
            logger.warning("check_drift failed for %s: %s", package_id, exc)
            return {
                "drifted": True,
                "current_hash": "",
                "stored_hash": row["hash"],
                "install_dir": str(install_dir),
                "error": str(exc),
            }

        return {
            "drifted": current != row["hash"],
            "current_hash": current,
            "stored_hash": row["hash"],
            "install_dir": str(install_dir),
        }

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        from digitorn.core.packages.resolver import _app_dir
        owner = getattr(row, "owner_user_id", None) or None
        install_dir = _app_dir(row.package_id, user_id=owner)
        return {
            "id": getattr(row, "id", None),
            "package_id": row.package_id,
            "scope": getattr(row, "scope", "system") or "system",
            "owner_user_id": getattr(row, "owner_user_id", None),
            "source_type": row.source_type,
            "source_uri": row.source_uri,
            "version": row.version,
            "hash": row.hash,
            "install_dir": str(install_dir),
            "manifest": dict(row.manifest or {}),
            "status": row.status,
            "last_error": row.last_error,
            "installed_at": row.installed_at.isoformat() if row.installed_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "installed_by": row.installed_by or "",
        }
