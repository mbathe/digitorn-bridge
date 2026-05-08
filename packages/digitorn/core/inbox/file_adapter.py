"""File-backed adapter that exposes the ``InboxStore`` API on top of
``FileInboxStore`` (per-user JSON files on disk).

Why an adapter, not a refactor: the daemon's ``InboxProducer``,
``NotificationDispatcher`` and ``api/user.py`` routes were written
against a Postgres-shaped ``InboxStore`` (``create_item`` returns
``dict``, kwargs use ``metadata=``). The new ``FileInboxStore`` was
written with a cleaner native API (``add()`` returns dataclass,
kwargs use ``item_metadata=``). Reconciling them at the call sites
would touch dozens of files for zero behavioural change. An adapter
lets the daemon load ``FileInboxStore`` for self-hosted runtimes
WITHOUT touching the API layer.

Activated automatically by ``server.py`` lifespan when
``settings.database.url`` is empty (= no Postgres = self-hosted).

Coverage: every method ``InboxProducer`` and the API routes call -
``create_item``, ``list_for_user``, ``count_unread``, ``mark_read``,
``mark_all_read``, ``archive``, ``prune_old``. Device tokens and
push-notification prefs are NOT supported (push needs a server-side
relay anyway; self-hosted users are typically on a single device).
The methods are no-op'd to keep the API contract intact.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from digitorn.core.runtime.session_store.inbox_store import (
    FileInboxStore, InboxItem,
)

logger = logging.getLogger(__name__)


def _item_to_dict(item: InboxItem) -> dict[str, Any]:
    """Mirror ``inbox.store._item_to_dict`` so the API serialisation
    is byte-identical between Postgres and file backends."""
    return {
        "id": item.id,
        "user_id": item.user_id,
        "kind": item.kind,
        "title": item.title,
        "subtitle": item.subtitle,
        "app_id": item.app_id,
        "session_id": item.session_id,
        "activation_id": item.activation_id,
        "credential_provider": item.credential_provider,
        "metadata": dict(item.item_metadata or {}),
        "created_at": item.created_at,
        "read_at": item.read_at,
        "archived_at": item.archived_at,
    }


class InboxStoreFileAdapter:
    """Drop-in replacement for ``InboxStore`` that persists per-user
    notifications as JSON files under ``<root>/<user_id>/<item_id>.json``.

    Method shapes (signatures, return types, and live SocketIO emits)
    are identical to the Postgres ``InboxStore`` so the daemon's
    ``InboxProducer`` and the ``/api/users/me/inbox`` routes work
    without any branching at the call sites.
    """

    def __init__(
        self,
        *,
        root: Path,
        sio: Any | None = None,
    ) -> None:
        self._store = FileInboxStore(root=root)
        # Late-bound by the lifespan: the SocketIO server isn't
        # constructed yet when the store is built.
        self._sio = sio

    # ── DI hook used by server.py lifespan ───────────────────────────
    def attach_sio(self, sio: Any) -> None:
        self._sio = sio

    async def _emit_user(
        self, user_id: str, event: str, payload: Any,
    ) -> None:
        """Best-effort live emit to ``user:<uid>``. Same behaviour as
        the Postgres store: any failure is debug-logged, never raised.
        """
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

    # ── Items ────────────────────────────────────────────────────────

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
        item = await self._store.add(
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
        d = _item_to_dict(item)
        await self._emit_user(user_id, "inbox.created", d)
        return d

    async def list_for_user(
        self,
        *,
        user_id: str,
        limit: int = 100,
        since_id: str | None = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        items = await self._store.list(
            user_id=user_id,
            include_archived=include_archived,
            limit=max(limit, 1) if since_id is None else limit + 200,
        )
        if since_id:
            cursor = next(
                (i for i in items if i.id == since_id), None,
            )
            if cursor is not None:
                # FileInboxStore.list() sorts by created_at desc, so
                # "older than cursor" = entries AFTER it in the list.
                cursor_idx = items.index(cursor)
                items = items[cursor_idx + 1:]
        return [_item_to_dict(i) for i in items[:limit]]

    async def count_unread(self, *, user_id: str) -> int:
        return await self._store.count_unread(user_id=user_id)

    async def mark_read(
        self, *, user_id: str, item_id: str,
    ) -> bool:
        ok = await self._store.mark_read(
            user_id=user_id, item_id=item_id,
        )
        if ok:
            await self._emit_user(
                user_id, "inbox.read", {"id": item_id},
            )
        return ok

    async def mark_all_read(self, *, user_id: str) -> int:
        items = await self._store.list(
            user_id=user_id, unread_only=True,
            include_archived=False, limit=10_000,
        )
        marked = 0
        for it in items:
            if await self._store.mark_read(
                user_id=user_id, item_id=it.id,
            ):
                marked += 1
        if marked > 0:
            await self._emit_user(
                user_id, "inbox.read_all", {"count": marked},
            )
        return marked

    async def archive(
        self, *, user_id: str, item_id: str,
    ) -> bool:
        ok = await self._store.archive(
            user_id=user_id, item_id=item_id,
        )
        if ok:
            await self._emit_user(
                user_id, "inbox.archived", {"id": item_id},
            )
        return ok

    async def prune_old(
        self, *, older_than_days: int = 30,
    ) -> int:
        """Walk every user's archived items and delete those older
        than the cutoff. Slow when there are millions of users on
        the same disk -- but that's what the cloud Postgres backend
        is for. A self-hosted single user has at most a few hundred
        items so this is sub-second.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=older_than_days,
        )
        cutoff_iso = cutoff.isoformat()
        n = 0
        root = self._store.root
        if not root.exists():
            return 0
        for user_dir in root.iterdir():
            if not user_dir.is_dir():
                continue
            user_id = user_dir.name
            items = await self._store.list(
                user_id=user_id, include_archived=True, limit=10_000,
            )
            for it in items:
                if it.archived_at is None:
                    continue
                if it.archived_at < cutoff_iso:
                    if await self._store.delete(
                        user_id=user_id, item_id=it.id,
                    ):
                        n += 1
        return n

    # ── Devices / push notifs ──────────────────────────────────────
    # No-op in file mode: a self-hosted runtime has no FCM relay.
    # Returning empty data keeps the API contract intact so the
    # client gracefully shows "no devices" instead of erroring.

    async def register_device(
        self,
        *,
        user_id: str,
        platform: str,
        fcm_token: str,
        device_name: str = "",
        app_version: str = "",
    ) -> dict[str, Any]:
        return {
            "id": "file-mode-no-push",
            "user_id": user_id,
            "platform": platform,
            "fcm_token": fcm_token,
            "device_name": device_name,
            "app_version": app_version,
            "active": False,
        }

    async def unregister_device(
        self, *, user_id: str, device_id: str,
    ) -> bool:
        return False

    async def list_devices(
        self, *, user_id: str,
    ) -> list[dict[str, Any]]:
        return []

    async def get_notification_prefs(
        self, *, user_id: str,
    ) -> dict[str, Any] | None:
        return None

    async def save_notification_prefs(
        self, *, user_id: str, prefs: dict[str, Any],
    ) -> dict[str, Any]:
        return dict(prefs)
