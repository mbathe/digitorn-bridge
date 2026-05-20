"""InboxStore - async CRUD over the inbox tables."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


class InboxStore:
    def __init__(self, session_factory: Any, sio: Any | None = None) -> None:
        self._session_factory = session_factory
        self._sio = sio

    def attach_sio(self, sio: Any) -> None:
        """Late-bind the Socket.IO server."""
        self._sio = sio

    async def _emit_user(self, user_id: str, event: str, payload: Any) -> None:
        """Best-effort live emit to `user:<uid>`."""
        if self._sio is None or not user_id:
            return
        try:
            await self._sio.emit(
                event, payload,
                room=f"user:{user_id}", namespace="/events",
            )
        except Exception as exc:
            logger.debug(
                "inbox_live_emit_failed event=%s user=%s: %s",
                event, user_id, exc,
            )


    async def create_item(
        self,
        *,
        user_id: str,
        kind: str,
        title: str,
        subtitle: str = "",
        app_id: str | None = None,
        session_id: str | None = None,
        activation_id: str | None = None,
        credential_provider: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a new inbox item. Returns the stored row as a dict.

        Fans out `inbox.created` to `user:<uid>` after commit so
        the client's bell badge updates live (only delivered when the
        user is NOT currently joined to a session room - matches the
        strict isolation contract in `socketio_bus.on_join_session`).
        """
        from digitorn.core.models import InboxItem

        async with self._session_factory() as db:
            row = InboxItem(
                user_id=user_id,
                kind=kind,
                title=title[:255],
                subtitle=subtitle or "",
                app_id=app_id,
                session_id=session_id,
                activation_id=activation_id,
                credential_provider=credential_provider,
                item_metadata=dict(metadata or {}),
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            item = _item_to_dict(row)

        await self._emit_user(user_id, "inbox.created", item)
        return item

    async def list_for_user(
        self,
        *,
        user_id: str,
        limit: int = 100,
        since_id: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """Return items newest first. Cursor = last id returned."""
        from digitorn.core.models import InboxItem

        async with self._session_factory() as db:
            stmt = select(InboxItem).where(InboxItem.user_id == user_id)
            if not include_archived:
                stmt = stmt.where(InboxItem.archived_at.is_(None))
            if since_id:
                # Find the cursor row's created_at, then return older
                cursor_row = (
                    await db.execute(
                        select(InboxItem).where(InboxItem.id == since_id)
                    )
                ).scalar_one_or_none()
                if cursor_row is not None:
                    stmt = stmt.where(
                        InboxItem.created_at < cursor_row.created_at
                    )
            stmt = stmt.order_by(InboxItem.created_at.desc()).limit(limit)
            rows = (await db.execute(stmt)).scalars().all()
            return [_item_to_dict(r) for r in rows]

    async def count_unread(self, *, user_id: str) -> int:
        from digitorn.core.models import InboxItem
        from sqlalchemy import func
        async with self._session_factory() as db:
            stmt = select(func.count()).select_from(InboxItem).where(
                InboxItem.user_id == user_id,
                InboxItem.read_at.is_(None),
                InboxItem.archived_at.is_(None),
            )
            return int((await db.execute(stmt)).scalar() or 0)

    async def mark_read(self, *, user_id: str, item_id: str) -> bool:
        from digitorn.core.models import InboxItem
        changed = False
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(InboxItem).where(
                        InboxItem.id == item_id,
                        InboxItem.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            if row.read_at is None:
                row.read_at = datetime.now(timezone.utc)
                await db.commit()
                changed = True
        if changed:
            await self._emit_user(user_id, "inbox.read", {"id": item_id})
        return True

    async def mark_all_read(self, *, user_id: str) -> int:
        from digitorn.core.models import InboxItem
        async with self._session_factory() as db:
            now = datetime.now(timezone.utc)
            result = await db.execute(
                update(InboxItem)
                .where(
                    InboxItem.user_id == user_id,
                    InboxItem.read_at.is_(None),
                    InboxItem.archived_at.is_(None),
                )
                .values(read_at=now)
            )
            await db.commit()
            count = int(result.rowcount or 0)
        if count > 0:
            await self._emit_user(
                user_id, "inbox.read_all", {"count": count},
            )
        return count

    async def archive(self, *, user_id: str, item_id: str) -> bool:
        from digitorn.core.models import InboxItem
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(InboxItem).where(
                        InboxItem.id == item_id,
                        InboxItem.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.archived_at = datetime.now(timezone.utc)
            await db.commit()
        await self._emit_user(user_id, "inbox.archived", {"id": item_id})
        return True

    async def prune_old(self, *, older_than_days: int = 30) -> int:
        """Delete archived items older than N days. Run periodically."""
        from digitorn.core.models import InboxItem
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        async with self._session_factory() as db:
            result = await db.execute(
                delete(InboxItem).where(
                    InboxItem.archived_at.is_not(None),
                    InboxItem.archived_at < cutoff,
                )
            )
            await db.commit()
            return int(result.rowcount or 0)


    async def register_device(
        self,
        *,
        user_id: str,
        platform: str,
        fcm_token: str,
        device_name: str = "",
        app_version: str = "",
    ) -> dict[str, Any]:
        """Upsert by (user_id, fcm_token) so reinstalls don't duplicate."""
        from digitorn.core.models import UserDevice

        async with self._session_factory() as db:
            stmt = select(UserDevice).where(
                UserDevice.user_id == user_id,
                UserDevice.fcm_token == fcm_token,
            )
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if existing is not None:
                existing.platform = platform
                existing.device_name = device_name or existing.device_name
                existing.app_version = app_version or existing.app_version
                existing.last_seen_at = datetime.now(timezone.utc)
                existing.active = True
                row = existing
            else:
                row = UserDevice(
                    user_id=user_id,
                    platform=platform,
                    fcm_token=fcm_token,
                    device_name=device_name,
                    app_version=app_version,
                )
                db.add(row)
            await db.commit()
            await db.refresh(row)
            return _device_to_dict(row)

    async def unregister_device(
        self, *, user_id: str, device_id: str,
    ) -> bool:
        """Soft-delete: flip `active=False` instead of removing the row."""
        from digitorn.core.models import UserDevice
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(UserDevice).where(
                        UserDevice.id == device_id,
                        UserDevice.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            row.active = False
            await db.commit()
            return True

    async def list_devices(
        self, *, user_id: str,
    ) -> list[dict[str, Any]]:
        from digitorn.core.models import UserDevice
        async with self._session_factory() as db:
            stmt = (
                select(UserDevice)
                .where(
                    UserDevice.user_id == user_id,
                    UserDevice.active.is_(True),
                )
                .order_by(UserDevice.last_seen_at.desc())
            )
            rows = (await db.execute(stmt)).scalars().all()
            return [_device_to_dict(r) for r in rows]


    async def get_notification_prefs(
        self, *, user_id: str,
    ) -> dict[str, Any] | None:
        from digitorn.core.models import UserDevice
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(UserDevice)
                    .where(
                        UserDevice.user_id == user_id,
                        UserDevice.active.is_(True),
                    )
                    .order_by(UserDevice.last_seen_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return dict(row.prefs or {})

    async def save_notification_prefs(
        self, *, user_id: str, prefs: dict[str, Any],
    ) -> dict[str, Any]:
        """Write prefs to every active device the user has."""
        from digitorn.core.models import UserDevice
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(UserDevice).where(
                        UserDevice.user_id == user_id,
                        UserDevice.active.is_(True),
                    )
                )
            ).scalars().all()
            if not rows:
                stub = UserDevice(
                    user_id=user_id,
                    platform="unknown",
                    fcm_token="",
                    prefs=dict(prefs),
                )
                db.add(stub)
            else:
                for row in rows:
                    row.prefs = dict(prefs)
                    flag_modified(row, "prefs")
            await db.commit()
            return dict(prefs)


def _item_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "subtitle": row.subtitle or "",
        "app_id": row.app_id,
        "session_id": row.session_id,
        "activation_id": row.activation_id,
        "credential_provider": row.credential_provider,
        "metadata": dict(row.item_metadata or {}),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "read_at": row.read_at.isoformat() if row.read_at else None,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
    }


def _device_to_dict(row: Any) -> dict[str, Any]:
    # `registered_at` is the v2 field; legacy callers expect
    # `created_at` so we expose both pointing at the same value.
    registered_at = getattr(row, "registered_at", None) or getattr(row, "created_at", None)
    return {
        "id": row.id,
        "platform": row.platform,
        "fcm_token": row.fcm_token,
        "device_name": row.device_name or "",
        "app_version": row.app_version or "",
        "created_at": registered_at.isoformat() if registered_at else None,
        "registered_at": registered_at.isoformat() if registered_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "prefs": dict(getattr(row, "prefs", None) or {}),
        "active": bool(getattr(row, "active", True)),
    }
