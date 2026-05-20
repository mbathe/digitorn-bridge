"""File-backed application + bundle registry."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

logger = logging.getLogger(__name__)


Scope = Literal["system", "user"]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_segment(value: str, label: str) -> None:
    if not value or "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"invalid {label}: {value!r}")


@dataclass
class Application:
    """Metadata for a deployed application. Mirrors the DB row."""

    id: str
    app_id: str
    scope: Scope = "system"
    owner_user_id: str = ""
    name: str = ""
    version: str = "1.0"
    description: str | None = None
    author: str = ""
    tags: list[str] = field(default_factory=list)
    current_bundle_id: str | None = None
    package_id: str | None = None
    source_type: str = "local"
    package_hash: str | None = None
    disabled: bool = False
    disabled_at: str | None = None
    created_at: str = field(default_factory=_utc_iso)
    updated_at: str = field(default_factory=_utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Application":
        return cls(
            id=str(d["id"]),
            app_id=str(d["app_id"]),
            scope=d.get("scope", "system"),
            owner_user_id=str(d.get("owner_user_id", "")),
            name=str(d.get("name", "")),
            version=str(d.get("version", "1.0")),
            description=d.get("description"),
            author=str(d.get("author", "")),
            tags=list(d.get("tags") or []),
            current_bundle_id=d.get("current_bundle_id"),
            package_id=d.get("package_id"),
            source_type=str(d.get("source_type", "local")),
            package_hash=d.get("package_hash"),
            disabled=bool(d.get("disabled", False)),
            disabled_at=d.get("disabled_at"),
            created_at=str(d.get("created_at", _utc_iso())),
            updated_at=str(d.get("updated_at", _utc_iso())),
        )


@dataclass
class AppBundle:
    """Metadata for one immutable bundle of an Application."""

    id: str
    app_id: str
    scope: Scope = "system"
    owner_user_id: str = ""
    bundle_hash: str = ""
    bundle_path: str = ""
    yaml_filename: str = "app.yaml"
    asset_count: int = 0
    size_bytes: int = 0
    created_at: str = field(default_factory=_utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AppBundle":
        return cls(
            id=str(d["id"]),
            app_id=str(d["app_id"]),
            scope=d.get("scope", "system"),
            owner_user_id=str(d.get("owner_user_id", "")),
            bundle_hash=str(d.get("bundle_hash", "")),
            bundle_path=str(d.get("bundle_path", "")),
            yaml_filename=str(d.get("yaml_filename", "app.yaml")),
            asset_count=int(d.get("asset_count", 0)),
            size_bytes=int(d.get("size_bytes", 0)),
            created_at=str(d.get("created_at", _utc_iso())),
        )


class FileAppRegistry:
    """Per-app registry on local disk."""

    def __init__(self, *, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _scope_dir(self, scope: Scope, owner_user_id: str) -> Path:
        if scope == "system":
            return self._root / "system"
        if scope == "user":
            _validate_segment(owner_user_id, "owner_user_id")
            return self._root / "users" / owner_user_id
        raise ValueError(f"invalid scope: {scope!r}")

    def _app_dir(
        self, *, scope: Scope, owner_user_id: str, app_id: str,
    ) -> Path:
        _validate_segment(app_id, "app_id")
        return self._scope_dir(scope, owner_user_id) / app_id

    async def register_app(
        self,
        *,
        app_id: str,
        scope: Scope = "system",
        owner_user_id: str = "",
        name: str = "",
        version: str = "1.0",
        description: str | None = None,
        author: str = "",
        tags: list[str] | None = None,
        current_bundle_id: str | None = None,
        package_id: str | None = None,
        source_type: str = "local",
        package_hash: str | None = None,
    ) -> Application:
        """Create or upsert an application registry entry. If an entry"""
        existing = await self.get_app(
            app_id=app_id, scope=scope, owner_user_id=owner_user_id,
        )
        if existing is None:
            app = Application(
                id=uuid.uuid4().hex,
                app_id=app_id,
                scope=scope,
                owner_user_id=owner_user_id,
                name=name,
                version=version,
                description=description,
                author=author,
                tags=list(tags or []),
                current_bundle_id=current_bundle_id,
                package_id=package_id,
                source_type=source_type,
                package_hash=package_hash,
            )
        else:
            app = existing
            app.name = name or app.name
            app.version = version or app.version
            if description is not None:
                app.description = description
            app.author = author or app.author
            if tags is not None:
                app.tags = list(tags)
            if current_bundle_id is not None:
                app.current_bundle_id = current_bundle_id
            if package_id is not None:
                app.package_id = package_id
            if source_type:
                app.source_type = source_type
            if package_hash is not None:
                app.package_hash = package_hash
            app.updated_at = _utc_iso()
        await asyncio.to_thread(self._write_app, app)
        return app

    async def get_app(
        self,
        *,
        app_id: str,
        scope: Scope = "system",
        owner_user_id: str = "",
    ) -> Application | None:
        return await asyncio.to_thread(
            self._read_app, scope, owner_user_id, app_id,
        )

    async def list_apps(
        self,
        *,
        scope: Scope | None = None,
        owner_user_id: str | None = None,
        include_disabled: bool = False,
    ) -> list[Application]:
        """List apps. `scope=None` returns all (system + every user)."""
        return await asyncio.to_thread(
            self._list_apps_sync, scope, owner_user_id, include_disabled,
        )

    async def update_app(
        self,
        *,
        app_id: str,
        scope: Scope = "system",
        owner_user_id: str = "",
        **fields: Any,
    ) -> Application | None:
        """Patch known fields. Unknown fields are silently ignored"""
        existing = await self.get_app(
            app_id=app_id, scope=scope, owner_user_id=owner_user_id,
        )
        if existing is None:
            return None
        allowed = {
            "name", "version", "description", "author", "tags",
            "current_bundle_id", "package_id", "source_type",
            "package_hash", "disabled", "disabled_at",
        }
        for key, value in fields.items():
            if key in allowed:
                setattr(existing, key, value)
        existing.updated_at = _utc_iso()
        await asyncio.to_thread(self._write_app, existing)
        return existing

    async def disable_app(
        self,
        *,
        app_id: str,
        scope: Scope = "system",
        owner_user_id: str = "",
    ) -> bool:
        return await self.update_app(
            app_id=app_id, scope=scope, owner_user_id=owner_user_id,
            disabled=True, disabled_at=_utc_iso(),
        ) is not None

    async def enable_app(
        self,
        *,
        app_id: str,
        scope: Scope = "system",
        owner_user_id: str = "",
    ) -> bool:
        return await self.update_app(
            app_id=app_id, scope=scope, owner_user_id=owner_user_id,
            disabled=False, disabled_at=None,
        ) is not None

    async def delete_app(
        self,
        *,
        app_id: str,
        scope: Scope = "system",
        owner_user_id: str = "",
    ) -> bool:
        """Hard delete the registry entry. The bundle CONTENT on disk"""
        return await asyncio.to_thread(
            self._delete_app_sync, scope, owner_user_id, app_id,
        )

    async def register_bundle(
        self,
        *,
        app_id: str,
        scope: Scope = "system",
        owner_user_id: str = "",
        bundle_hash: str,
        bundle_path: str,
        yaml_filename: str = "app.yaml",
        asset_count: int = 0,
        size_bytes: int = 0,
        bundle_id: str | None = None,
        set_current: bool = True,
    ) -> AppBundle:
        """Persist a new bundle. If `set_current=True` the parent"""
        bundle = AppBundle(
            id=bundle_id or uuid.uuid4().hex,
            app_id=app_id,
            scope=scope,
            owner_user_id=owner_user_id,
            bundle_hash=bundle_hash,
            bundle_path=bundle_path,
            yaml_filename=yaml_filename,
            asset_count=asset_count,
            size_bytes=size_bytes,
        )
        await asyncio.to_thread(self._append_bundle, bundle)
        if set_current:
            await self.update_app(
                app_id=app_id, scope=scope, owner_user_id=owner_user_id,
                current_bundle_id=bundle.id,
            )
        return bundle

    async def list_bundles(
        self,
        *,
        app_id: str,
        scope: Scope = "system",
        owner_user_id: str = "",
    ) -> list[AppBundle]:
        return await asyncio.to_thread(
            self._read_bundles, scope, owner_user_id, app_id,
        )

    async def get_bundle(
        self,
        *,
        app_id: str,
        bundle_id: str,
        scope: Scope = "system",
        owner_user_id: str = "",
    ) -> AppBundle | None:
        bundles = await self.list_bundles(
            app_id=app_id, scope=scope, owner_user_id=owner_user_id,
        )
        for b in bundles:
            if b.id == bundle_id:
                return b
        return None

    def _app_files(
        self, scope: Scope, owner_user_id: str, app_id: str,
    ) -> tuple[Path, Path]:
        ad = self._app_dir(
            scope=scope, owner_user_id=owner_user_id, app_id=app_id,
        )
        return ad / "application.json", ad / "bundles.json"

    def _read_app(
        self, scope: Scope, owner_user_id: str, app_id: str,
    ) -> Application | None:
        try:
            app_path, _ = self._app_files(scope, owner_user_id, app_id)
        except ValueError:
            return None
        if not app_path.exists():
            return None
        try:
            return Application.from_dict(
                json.loads(app_path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning(
                "app_registry_corrupt path=%s err=%s", app_path, exc,
            )
            return None

    def _write_app(self, app: Application) -> None:
        app_path, _ = self._app_files(
            app.scope, app.owner_user_id, app.app_id,
        )
        _atomic_write_json(app_path, app.to_dict())

    def _delete_app_sync(
        self, scope: Scope, owner_user_id: str, app_id: str,
    ) -> bool:
        try:
            ad = self._app_dir(
                scope=scope, owner_user_id=owner_user_id, app_id=app_id,
            )
        except ValueError:
            return False
        if not ad.exists():
            return False
        for entry in ad.iterdir():
            try:
                entry.unlink()
            except (IsADirectoryError, OSError):
                pass
        try:
            ad.rmdir()
            return True
        except OSError:
            return False

    def _list_apps_sync(
        self,
        scope: Scope | None,
        owner_user_id: str | None,
        include_disabled: bool,
    ) -> list[Application]:
        out: list[Application] = []
        for app in self._iter_all_apps(scope, owner_user_id):
            if not include_disabled and app.disabled:
                continue
            out.append(app)
        out.sort(key=lambda a: (a.scope, a.owner_user_id, a.app_id))
        return out

    def _iter_all_apps(
        self,
        scope_filter: Scope | None,
        owner_filter: str | None,
    ) -> Iterable[Application]:
        if scope_filter in (None, "system"):
            sys_dir = self._root / "system"
            if sys_dir.exists():
                yield from self._iter_apps_in_dir(sys_dir, "system", "")
        if scope_filter in (None, "user"):
            users_dir = self._root / "users"
            if users_dir.exists():
                if owner_filter is not None:
                    user_dir = users_dir / owner_filter
                    if user_dir.exists():
                        yield from self._iter_apps_in_dir(
                            user_dir, "user", owner_filter,
                        )
                else:
                    for entry in users_dir.iterdir():
                        if entry.is_dir():
                            yield from self._iter_apps_in_dir(
                                entry, "user", entry.name,
                            )

    def _iter_apps_in_dir(
        self, base: Path, scope: Scope, owner_user_id: str,
    ) -> Iterable[Application]:
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            app_path = entry / "application.json"
            if not app_path.exists():
                continue
            try:
                app = Application.from_dict(
                    json.loads(app_path.read_text(encoding="utf-8"))
                )
            except (json.JSONDecodeError, OSError, KeyError) as exc:
                logger.warning(
                    "app_registry_skip_bad path=%s err=%s",
                    app_path, exc,
                )
                continue
            yield app

    def _append_bundle(self, bundle: AppBundle) -> None:
        _, bundles_path = self._app_files(
            bundle.scope, bundle.owner_user_id, bundle.app_id,
        )
        records: list[dict[str, Any]] = []
        if bundles_path.exists():
            try:
                records = json.loads(bundles_path.read_text(encoding="utf-8"))
                if not isinstance(records, list):
                    records = []
            except (json.JSONDecodeError, OSError):
                records = []
        records = [r for r in records if r.get("id") != bundle.id]
        records.append(bundle.to_dict())
        _atomic_write_json(bundles_path, records)

    def _read_bundles(
        self, scope: Scope, owner_user_id: str, app_id: str,
    ) -> list[AppBundle]:
        try:
            _, bundles_path = self._app_files(scope, owner_user_id, app_id)
        except ValueError:
            return []
        if not bundles_path.exists():
            return []
        try:
            raw = json.loads(bundles_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "bundles_corrupt path=%s err=%s", bundles_path, exc,
            )
            return []
        if not isinstance(raw, list):
            return []
        out: list[AppBundle] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            try:
                out.append(AppBundle.from_dict(r))
            except (KeyError, ValueError, TypeError) as exc:
                logger.warning(
                    "bundle_record_corrupt err=%s record=%s", exc, r,
                )
        out.sort(key=lambda b: b.created_at)
        return out


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".reg_", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
